"""History service on the fake api-server, with a fake engine/runtime and broadcaster."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from copperchat.config import Settings
from copperchat.db import Database
from copperchat.history.hooks import HistoryHooks
from copperchat.history.service import HistoryService
from copperchat.kicad_session import KicadSession, KicadSessionManager
from copperchat.projects import Project
from tests.support.fake_kicad import FakeClientFactory, FakeLauncher, FakeServer
from tests.support.history import ECC83, FakeEngine, FakeRuntime, RecordingBroadcaster, copy_project
from tests.support.conversations import ensure_test_conversation


@dataclass
class Env:
    svc: HistoryService
    sessions: KicadSessionManager
    server: FakeServer
    engine: FakeEngine
    broadcaster: RecordingBroadcaster
    settings: Settings
    written: list[str] = field(default_factory=list)
    saved: list[str] = field(default_factory=list)
    save_error: Exception | None = None
    save_error_doc: str | None = None  # None: every save fails

    def project(self, src: Path = ECC83) -> Project:
        return copy_project(src, self.settings.projects_root)


@pytest.fixture
async def env(
    tmp_path: Path, make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Env]:
    server = FakeServer()
    settings = make_settings()
    sessions = KicadSessionManager(
        settings, FakeLauncher(server), client_factory=FakeClientFactory(server)
    )
    db = await Database.open(tmp_path / "db.sqlite")
    await ensure_test_conversation(db)
    engine = FakeEngine()
    broadcaster = RecordingBroadcaster()
    svc = HistoryService(
        db=db, sessions=sessions, broadcaster=broadcaster, runtime=FakeRuntime(engine)
    )
    e = Env(svc, sessions, server, engine, broadcaster, settings)

    async def write_view(session: Any, doc: str) -> Path:
        e.written.append(doc)
        return tmp_path / doc

    async def preview(session: Any, doc: str) -> bytes:
        return b"\x89PNG"

    real_save = KicadSession.save

    async def recording_save(self: KicadSession, doc: Any) -> None:
        if e.save_error is not None and e.save_error_doc in (None, doc):
            raise e.save_error
        await real_save(self, doc)  # read-only check + FakeDocument.save()
        e.saved.append(doc)

    monkeypatch.setattr(KicadSession, "save", recording_save)
    svc.write_view = write_view  # type: ignore[assignment]
    svc.render_preview = preview  # type: ignore[assignment]
    yield e
    await sessions.close_all()
    await db.close()
    server.release.set()


@dataclass
class Opened:
    env: Env
    project: Project
    session: KicadSession
    hooks: HistoryHooks


@pytest.fixture
async def opened(env: Env) -> Opened:
    project = env.project()
    session = await env.sessions.open(project)
    hooks = env.svc.hooks_factory(project, env.engine)  # type: ignore[arg-type]
    await hooks.on_engine_start()
    return Opened(env, project, session, hooks)
