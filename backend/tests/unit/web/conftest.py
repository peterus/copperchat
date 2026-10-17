"""Minimal app with the chat routers and a FakeAgent runtime (Task 5.13 wires the real one)."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from copperchat.agent.broadcaster import Broadcaster
from copperchat.agent.engine import RunEngine
from copperchat.agent.fake import FakeAgent, ScriptFor
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.projects import Project, project_id_for
from copperchat.web import routes_messages, ws
from copperchat.web.app import install_error_handlers
from copperchat.web.auth import User, require_user


class StubRuntime:
    def __init__(
        self, settings: Settings, db: Database, script_for: ScriptFor | None, max_queue: int
    ) -> None:
        self.settings, self.db, self.script_for = settings, db, script_for
        self.broadcaster = Broadcaster(max_queue=max_queue)
        self.engines: dict[str, RunEngine] = {}

    def get(self, project_id: str) -> RunEngine | None:
        return self.engines.get(project_id)

    async def ensure(self, project: Project) -> RunEngine:
        if project.id not in self.engines:
            engine = RunEngine(
                project=project,
                settings=self.settings,
                db=self.db,
                broadcaster=self.broadcaster,
                sessions=cast(Any, SimpleNamespace(current=lambda: None)),
                backend_factory=lambda ctx: FakeAgent(self.script_for, ctx_factory=ctx),
            )
            await engine.start()
            self.engines[project.id] = engine
        return self.engines[project.id]


def _build_chat_app(
    tmp_path: Path,
    script_for: ScriptFor | None = None,
    *,
    auth: bool = False,
    max_queue: int = 1000,
) -> tuple[FastAPI, str]:
    root = tmp_path / "projects"
    for name in ("demo", "other"):
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / f"{name}.kicad_pro").write_text("")  # discovery marker only
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        projects_root=root,
        data_dir=tmp_path / "data",
        claude_code_oauth_token=SecretStr("t"),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = await Database.open(tmp_path / "t.db")
        runtime = StubRuntime(settings, db, script_for, max_queue)
        app.state.settings, app.state.db, app.state.agent_runtime = settings, db, runtime
        yield
        for engine in runtime.engines.values():
            await engine.close()
        await db.close()

    app = FastAPI(lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(routes_messages.router)
    app.include_router(ws.router)
    if not auth:
        app.dependency_overrides[require_user] = lambda: User(id=1, username="admin")
    return app, project_id_for("demo/demo.kicad_pro")


OTHER_PID = project_id_for("other/other.kicad_pro")


@pytest.fixture
def build_chat_app(tmp_path: Path) -> Callable[..., tuple[FastAPI, str]]:
    def build(script_for: ScriptFor | None = None, **kw: Any) -> tuple[FastAPI, str]:
        return _build_chat_app(tmp_path, script_for, **kw)

    return build
