"""Board (SP4) tests against a real KiCad api-server."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio

from copperchat.config import Settings
from copperchat.db import Database
from copperchat.history.service import HistoryService
from copperchat.kicad_session import KicadSession, KicadSessionManager
from copperchat.projects import Project, discover_projects, get_project
from copperchat.tools.spec import ToolContext
from tests.support.edit import edit_ctx
from tests.support.kicad_env import KICAD_CLI
from tests.support.kicad_project import copy_fixture, real_settings


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def ecc83_module_session(
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[KicadSession]:
    """One ecc83 api-server for a whole test module (tests that only read, or whose
    changes the later tests of the module tolerate). Tests using it run on the module
    event loop: `pytest.mark.asyncio(loop_scope="module")`."""
    tmp: Path = tmp_path_factory.mktemp("ecc83-module")
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        projects_root=tmp / "projects",
        data_dir=tmp / "data",
        admin_password="test-password",
        claude_code_oauth_token="test-token",
        cookie_secure=False,
        frontend_dist=tmp / "no-frontend",
        kicad_cli=KICAD_CLI,
        kicad_api_timeout_s=30.0,
        kicad_start_timeout_s=60.0,
    )
    settings.projects_root.mkdir(parents=True)
    copy_fixture("ecc83", settings.projects_root)
    project = next(p for p in discover_projects(settings.projects_root) if p.name == "ecc83-pp")
    manager = KicadSessionManager(settings)
    try:
        yield await manager.open(project)
    finally:
        await manager.close_all()


# -- board_ctx (SP4 P1a Task 7; reused by later phases) -----------------------------------


class _OneSession:
    """The fixture's session as the 'current' one of a HistoryService (no reopen)."""

    def __init__(self, session: KicadSession) -> None:
        self.session = session

    def current(self) -> KicadSession:
        return self.session

    def add_save_listener(self, fn: Any) -> None:
        return None


async def _no_preview(session: Any, doc: Any) -> None:
    return None


def board_ctx(
    session: KicadSession,
    db: Database,
    sessions: KicadSessionManager | None = None,
) -> ToolContext:
    """A board tool context as `RunEngine._make_ctx` builds it: `edit_ctx` (editing
    service, run id) plus a real `HistoryService`; `on_mutation` takes the turn
    snapshot, and - with the `sessions` manager that opened `session` - `reload_project`
    re-discovers the project (`get_project`) and reopens it (`sessions.reopen`),
    returning the new session's generation read at once. Without `sessions` there is no
    reload (board creation then refuses)."""
    svc = HistoryService(
        db=db,
        sessions=cast(Any, sessions if sessions is not None else _OneSession(session)),
        broadcaster=None,
        runtime=None,
    )
    svc.render_preview = _no_preview  # type: ignore[assignment]
    root = session.settings.projects_root
    svc.projects_root = root
    ctx = edit_ctx(session, db)

    async def on_mutation() -> None:
        await svc.store(ctx.project).snapshot(ctx.run_id or "run", "turn")

    ctx.on_mutation = on_mutation
    if sessions is not None:

        async def reload_project() -> tuple[Project, KicadSession, int]:
            fresh = get_project(root, ctx.project.id)
            new = await sessions.reopen(fresh)
            return fresh, new, new.generation

        ctx.reload_project = reload_project
    return ctx


@pytest_asyncio.fixture
async def board_db(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.open(tmp_path / "board.db")
    try:
        yield db
    finally:
        await db.close()


@pytest_asyncio.fixture
async def blank_nopcb_sessions(
    make_settings: Callable[..., Settings],
) -> AsyncIterator[KicadSessionManager]:
    """The blank project copied without its .kicad_pcb, opened through a manager that
    `board_ctx(..., sessions=)` can reopen with (the manager's `current()` is the
    session, as `blank_nopcb_session` would give it)."""
    settings = real_settings(make_settings)
    dest = copy_fixture("blank", settings.projects_root)
    for pcb in dest.glob("*.kicad_pcb"):
        pcb.unlink()
    project = next(p for p in discover_projects(settings.projects_root) if p.name == "blank")
    manager = KicadSessionManager(settings)
    try:
        await manager.open(project)
        yield manager
    finally:
        await manager.close_all()


# -- synth_board (SP4 P3a; reused by later phases) -----------------------------------------


def _build_synth_board(projects_root: Path) -> Path:
    """`sp4_synth` copied to `projects_root` with a board made by KiCad (P1b's
    `test_netlist_imports_cleanly`, not dry): kicad-cli netlist → CreateDocument(PCB) +
    save → reopen → `import_netlist(dry_run=False)` → save. API only; footprints stay where
    KiCad puts them. Returns the project directory."""
    import subprocess

    from kipy.proto.common.types import DocumentType

    from tests.kicad.board.test_sp4_synth_build import NAME, standalone_server

    work = copy_fixture(NAME, projects_root)
    netlist = projects_root.parent / f"{NAME}.net"
    subprocess.run(
        [KICAD_CLI, "sch", "export", "netlist", "--format", "kicadsexpr", "-o", str(netlist),
         str(work / f"{NAME}.kicad_sch")],
        check=True, capture_output=True, timeout=120,
    )  # fmt: skip
    pcb = work / f"{NAME}.kicad_pcb"
    with standalone_server(KICAD_CLI) as client:
        client.create_document(str(pcb), DocumentType.DOCTYPE_PCB)
        client.get_board().save()
        for doc in client.get_open_documents(DocumentType.DOCTYPE_PCB):
            client.close_document(doc)
        client.open_document(str(pcb), DocumentType.DOCTYPE_PCB)
        board = client.get_board()
        result = board.import_netlist(str(netlist), dry_run=False)
        assert result.error_count == 0, result.report
        board.save()
    return work


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def synth_board(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[KicadSession]:
    """The `sp4_synth` project with its netlist imported into a new board
    (`_build_synth_board`), opened as a normal CopperChat session (project with `pcb_file`)
    for a whole test module. Tests run on the module event loop
    (`pytest.mark.asyncio(loop_scope="module")`) and build their context per test:
    `edit_ctx(synth_board)` (read tools) or `board_ctx(synth_board, db)` (writes)."""
    tmp: Path = tmp_path_factory.mktemp("synth-board")
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        projects_root=tmp / "projects",
        data_dir=tmp / "data",
        admin_password="test-password",
        claude_code_oauth_token="test-token",
        cookie_secure=False,
        frontend_dist=tmp / "no-frontend",
        kicad_cli=KICAD_CLI,
        kicad_api_timeout_s=30.0,
        kicad_start_timeout_s=60.0,
    )
    settings.projects_root.mkdir(parents=True)
    _build_synth_board(settings.projects_root)
    project = next(p for p in discover_projects(settings.projects_root) if p.name == "sp4_synth")
    assert project.pcb_file is not None
    manager = KicadSessionManager(settings)
    try:
        yield await manager.open(project)
    finally:
        await manager.close_all()


# -- synth_rig (SP4 P2; sync / outline / rules on sp4_synth) ------------------------------


@dataclass
class SynthRig:
    """`sp4_synth` (schematic only) opened through a manager, with board tools on, a
    database with the test conversation and a HistoryService that can restore and
    reopen (`restore_turn`). `ctx()` builds a fresh board tool context on the current
    session (`board_ctx` with `sessions=`, so board creation reloads)."""

    sessions: KicadSessionManager
    db: Database
    history: HistoryService
    engine: Any
    settings: Settings

    @property
    def session(self) -> KicadSession:
        current = self.sessions.current()
        assert current is not None
        return current

    def ctx(self) -> ToolContext:
        return board_ctx(self.session, self.db, self.sessions)

    async def restore_turn(self, ctx: ToolContext) -> None:
        """History restore of the turn snapshot `ctx.on_mutation` took."""
        from copperchat.history.restore import restore_snapshot
        from tests.support.conversations import TEST_CID

        project = self.session.project
        snap = await self.history.store(project).latest_for_run(ctx.run_id or "run")
        assert snap is not None, "no turn snapshot"
        await restore_snapshot(self.history, project, snap.id, conversation_id=TEST_CID)


@pytest_asyncio.fixture
async def synth_rig(make_settings: Callable[..., Settings], tmp_path: Path) -> AsyncIterator[SynthRig]:
    from tests.support.conversations import ensure_test_conversation
    from tests.support.history import FakeEngine, FakeRuntime, RecordingBroadcaster

    settings = make_settings(
        kicad_cli=KICAD_CLI, kicad_api_timeout_s=30.0, kicad_start_timeout_s=60.0, board_tools=True
    )
    copy_fixture("sp4_synth", settings.projects_root)
    project = next(p for p in discover_projects(settings.projects_root) if p.name == "sp4_synth")
    assert project.pcb_file is None
    manager = KicadSessionManager(settings)
    db = await Database.open(tmp_path / "synth.db")
    await ensure_test_conversation(db)
    engine = FakeEngine()
    svc = HistoryService(
        db=db, sessions=manager, broadcaster=RecordingBroadcaster(), runtime=FakeRuntime(engine)
    )
    svc.projects_root = settings.projects_root
    try:
        await manager.open(project)
        yield SynthRig(manager, db, svc, engine, settings)
    finally:
        await manager.close_all()
        await db.close()
