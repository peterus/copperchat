"""The real app (create_app, dev_fake_agent) with a scripted FakeAgent per burst, for
the sessions C5 tests: the hooks run through the app's real ``hooks_factory``."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest_asyncio

from copperchat.agent.broadcaster import Subscription
from copperchat.agent.engine import RunEngine
from copperchat.agent.events import QueuedItem
from copperchat.agent.fake import FakeAgent, ScriptFor, Step, echo_script
from copperchat.agent.protocol import MessageDTO
from copperchat.config import Settings
from copperchat.db.repos.conversations import ConversationRepo, ConversationRow
from copperchat.db.repos.messages import MessageRepo, MessageRow
from copperchat.design_docs import frontmatter as fm
from copperchat.projects import Project, discover_projects
from copperchat.tools import registry
from copperchat.web.app import create_app
from copperchat.web.auth import User, require_user


class Env:
    def __init__(self, app: Any, client: httpx.AsyncClient, settings: Settings) -> None:
        self.app, self.client, self.settings = app, client, settings
        self.project: Project = discover_projects(settings.projects_root)[0]
        self.pid = self.project.id
        self.runtime = app.state.agent_runtime
        self.db = app.state.db
        self.docs = app.state.docs
        self.agents: list[FakeAgent] = []
        self.script: ScriptFor = echo_script
        self.extra_specs: list[Any] = []

        def factory(ctx: Callable[[], Any], turn: Callable[[], Any]) -> FakeAgent:
            agent = FakeAgent(
                lambda item: self.script(item),
                specs=[*registry.all_specs(), *self.extra_specs],
                ctx_factory=ctx,
                settings=settings,
                turn_context=turn,
            )
            self.agents.append(agent)
            return agent

        self.runtime.backend_factory = factory

    @property
    def design(self) -> Path:
        d = self.project.dir / "docs" / "design"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def agent(self) -> FakeAgent:
        return self.agents[0]

    async def engine(self) -> RunEngine:
        engine: RunEngine = await self.runtime.ensure(self.project)
        return engine

    async def create(self, kind: str, title: str | None = None) -> str:
        body: dict[str, Any] = {"type": kind}
        if title is not None:
            body["title"] = title
        r = await self.client.post(f"/api/projects/{self.pid}/conversations", json=body)
        assert r.status_code == 201, r.text
        return str(r.json()["id"])

    async def conv(self, cid: str) -> ConversationRow:
        row = await ConversationRepo(self.db).get(cid)
        assert row is not None
        return row

    async def send(self, cid: str, text: str, *, wait: bool = True) -> MessageDTO:
        engine = await self.engine()
        dto, _ = await engine.submit_ex(cid, f"m-{text[:20]}-{id(text)}", text, [])
        if wait:
            await self.idle()
        return dto

    async def idle(self, timeout: float = 5.0) -> None:
        engine = await self.engine()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            queued = await MessageRepo(self.db).queued_user_rows(self.pid)
            if not engine.is_busy and not queued and engine.rate_limited_until is None:
                await asyncio.sleep(0.01)
                if not engine.is_busy:
                    return
            if loop.time() > deadline:
                raise AssertionError("engine did not become idle")
            await asyncio.sleep(0.01)

    async def rows(self, cid: str) -> list[MessageRow]:
        rows, _ = await MessageRepo(self.db).page(self.pid, cid, limit=200)
        return rows

    def subscribe(self) -> Subscription:
        sub: Subscription = self.runtime.broadcaster.subscribe(self.pid)
        return sub

    def version(self, name: str) -> str | None:
        p = self.design / name
        if not p.exists():
            return None
        return fm.body_version(fm.split(p.read_text()).body)

    def by_conversation(self, scripts: dict[str, Callable[[QueuedItem], list[Step]]]) -> None:
        self.script = lambda item: scripts[item.conversation_id](item)


async def until(pred: Callable[[], Any], timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = pred()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return
        if loop.time() > deadline:
            raise AssertionError("condition not met")
        await asyncio.sleep(0.01)


@pytest_asyncio.fixture
async def env(make_settings: Callable[..., Settings]) -> AsyncIterator[Env]:
    settings = make_settings(dev_fake_agent=True)
    d = settings.projects_root / "board"
    d.mkdir(parents=True)
    (d / "board.kicad_pro").write_text("")
    app = create_app(settings)
    app.dependency_overrides[require_user] = lambda: User(id=1, username="admin")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            yield Env(app, client, settings)
