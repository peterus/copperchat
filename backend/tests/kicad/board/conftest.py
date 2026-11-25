"""Board (SP4) tests against a real KiCad api-server."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
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
