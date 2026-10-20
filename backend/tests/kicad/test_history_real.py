"""Phase 7 against a real api-server: autosave hashes, external change, restore."""

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from copperchat.agent.engine import TurnInfo, TurnOutcome
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.history.hooks import HistoryHooks
from copperchat.history.restore import restore_snapshot
from copperchat.history.service import HistoryService
from copperchat.history.store import compute_hashes
from copperchat.kicad_session import KicadSessionManager
from copperchat.projects import Project
from tests.support.conversations import TEST_CID, ensure_test_conversation
from tests.support.history import (
    ECC83,
    HIERARCHY,
    FakeEngine,
    FakeRuntime,
    RecordingBroadcaster,
    copy_project,
    replace_with_kicad_file,
)
from tests.support.kicad_project import real_settings

pytestmark = pytest.mark.kicad


@dataclass
class Real:
    svc: HistoryService
    sessions: KicadSessionManager
    engine: FakeEngine
    broadcaster: RecordingBroadcaster
    settings: Settings

    async def open(self, src: Path) -> tuple[Project, HistoryHooks]:
        project = copy_project(src, self.settings.projects_root)
        await self.sessions.open(project)
        hooks = self.svc.hooks_factory(project, self.engine)  # type: ignore[arg-type]
        await hooks.on_engine_start()
        return project, hooks


@pytest.fixture
async def real(make_settings: Callable[..., Settings], tmp_path: Path) -> AsyncIterator[Real]:
    settings = real_settings(make_settings)
    sessions = KicadSessionManager(settings)
    db = await Database.open(tmp_path / "db.sqlite")
    await ensure_test_conversation(db)
    engine, broadcaster = FakeEngine(), RecordingBroadcaster()
    svc = HistoryService(
        db=db, sessions=sessions, broadcaster=broadcaster, runtime=FakeRuntime(engine)
    )
    try:
        yield Real(svc, sessions, engine, broadcaster, settings)
    finally:
        await sessions.close_all()
        await db.close()


def turn(run_id: str) -> TurnInfo:
    return TurnInfo(
        run_id=run_id, message_id=1, text="tidy up", metadata={}, conversation_id="c" * 32
    )


async def _footprint_count(real: Real) -> int:
    session = real.sessions.current()
    assert session is not None
    return await session.call(lambda h: len(h.board().get_footprints()))


async def test_autosave_is_not_reported_as_external_change(real: Real) -> None:
    project, hooks = await real.open(ECC83)
    await hooks.on_turn_end(
        turn("r1"), TurnOutcome(reason="completed", changed_docs=frozenset({"sch", "pcb"}))
    )
    # save() normalises the files (spike 1C): the recorded hashes are the saved ones.
    assert await real.svc.hashes.get(project.id) == compute_hashes(project)
    frame = real.broadcaster.frames[-1]
    assert frame.type == "view-updated" and sorted(frame.docs) == ["pcb", "sch"]
    assert (project.dir / ".kicad-agent" / "view" / "ecc83-pp.kicad_pcb").is_file()
    await hooks.on_turn_start(turn("r2"))
    assert real.engine.notices == [] and real.engine.errors == []


async def test_external_change_reloads_real_session(real: Real) -> None:
    project, hooks = await real.open(ECC83)
    assert project.pcb_file is not None
    before = await _footprint_count(real)
    replace_with_kicad_file(project.pcb_file, next(HIERARCHY.glob("*.kicad_pcb")))
    await hooks.on_turn_start(turn("r1"))
    assert real.engine.notices[-1][1] == {
        "action": "external-change",
        "files": ["ecc83-pp.kicad_pcb"],
    }
    assert await _footprint_count(real) != before  # the agent sees the new board


async def test_snapshot_and_restore_hierarchical_project(real: Real) -> None:
    project, hooks = await real.open(HIERARCHY)
    original = compute_hashes(project)
    t = turn("r1")
    await hooks.on_first_mutation(t)  # real preview render via render_view
    snap = (await real.svc.store(project).list())[0]
    assert snap.has_preview and "ampli_ht.kicad_sch" in snap.files
    count = await _footprint_count(real)
    replace_with_kicad_file(project.dir / "ampli_ht.kicad_sch", next(ECC83.glob("*.kicad_sch")))
    assert project.pcb_file is not None
    replace_with_kicad_file(project.pcb_file, next(ECC83.glob("*.kicad_pcb")))

    await restore_snapshot(real.svc, project, snap.id, conversation_id=TEST_CID)

    assert compute_hashes(project) == original
    assert await _footprint_count(real) == count
    assert real.engine.notices[-1][1] == {"action": "restored", "snapshotId": snap.id}
    view = project.dir / ".kicad-agent" / "view"
    assert (view / "ampli_ht.kicad_sch").read_bytes() == (
        project.dir / "ampli_ht.kicad_sch"
    ).read_bytes()
