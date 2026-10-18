"""SP2 editing state, gate and change log (E-C5)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from copperchat.editing.service import ChangesRecorded, EditingService

if TYPE_CHECKING:
    from fastapi import FastAPI

    from copperchat.agent.engine import RunEngine, RunHooks
    from copperchat.projects import Project

__all__ = ["EditingService", "install_editing"]


def _publisher(broadcaster: Any) -> ChangesRecorded:
    """WS `changes-updated` after change rows were written (E-C12)."""
    from copperchat.agent.protocol import ChangesUpdatedFrame

    async def publish(project_id: str, run_id: str, step_id: str | None, count: int) -> None:
        await broadcaster.publish(
            project_id,
            ChangesUpdatedFrame(project_id=project_id, run_id=run_id, step_id=step_id, count=count),
        )

    return publish


def install_editing(app: FastAPI) -> EditingService:
    """Lifespan: after install_history, before resume_pending (engines keep the hooks
    and services they were created with). Sets `runtime.editing` (passed into every
    ToolContext) and chains EditingHooks after the history hooks."""
    from copperchat.agent.engine import ChainedHooks
    from copperchat.db.repos.editing_settings import EditingSettingsRepo
    from copperchat.editing.hooks import EditingHooks

    runtime = app.state.agent_runtime
    svc = EditingService(
        db=app.state.db,
        broadcaster=runtime.broadcaster,
        settings_repo=EditingSettingsRepo(app.state.db),
        history=getattr(app.state, "history", None),
    )
    svc.on_changes_recorded.append(_publisher(runtime.broadcaster))
    previous = runtime.hooks_factory

    def hooks_factory(project: Project, engine: RunEngine) -> RunHooks:
        return ChainedHooks([previous(project, engine), EditingHooks(svc, project, engine)])

    runtime.hooks_factory = hooks_factory
    runtime.editing = svc
    app.state.editing = svc
    return svc
