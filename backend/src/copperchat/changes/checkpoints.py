"""Plan-step checkpoints (E-C10, spec §5.4): save through the API, then snapshot."""

from __future__ import annotations

from typing import TYPE_CHECKING

from copperchat.errors import ReadOnlyProject
from copperchat.history.store import Snapshot
from copperchat.kicad_session import KicadUnavailable
from copperchat.projects import Project

if TYPE_CHECKING:
    from copperchat.history.service import HistoryService


async def take_step_checkpoint(
    svc: HistoryService, project: Project, run_id: str, step_id: str, label: str
) -> Snapshot:
    """`session.save("sch")` (the save listener records the hashes, so the next turn
    start never takes it for an external change) -> snapshot(kind="step"). Must not be
    called while holding `svc.lock(project)` or inside an open commit."""
    session = svc.open_session(project)
    if session is None:
        raise KicadUnavailable("the project is not open in KiCad")
    if session.read_only:
        raise ReadOnlyProject("project is open read-only; no checkpoint is taken")
    async with svc.lock(project):
        if project.sch_file is not None:
            await session.save("sch")
        preview = await svc.render_preview(session, "sch" if project.sch_file else "pcb")
        return await svc.store(project).snapshot(
            run_id, label, preview, kind="step", plan_step_id=step_id
        )
