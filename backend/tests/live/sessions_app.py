"""The real app (``create_app``, the SDK backend, the built-in skills) for the opt-in
sessions live tests: sessions are created and docs approved through REST, messages go
through the project's ``RunEngine`` as the WS handler sends them, and the chat rows are
read back from the database (spec §14.4)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from copperchat.agent.engine import RunEngine
from copperchat.config import Settings
from copperchat.db.repos.conversations import ConversationRepo
from copperchat.db.repos.messages import MessageRepo, MessageRow
from copperchat.projects import Project, discover_projects
from copperchat.web.app import create_app
from copperchat.web.auth import User, require_user

from .conftest import live_settings


@dataclass
class Turn:
    """What happened in one conversation, from its persisted rows."""

    rows: list[MessageRow] = field(default_factory=list)

    @property
    def tool_uses(self) -> list[MessageRow]:
        return [r for r in self.rows if r.kind == "tool_use"]

    def main_tools(self) -> list[str]:
        return [r.content for r in self.tool_uses if not r.metadata.get("parentToolUseId")]

    def sub_tools(self) -> list[MessageRow]:
        return [r for r in self.tool_uses if r.metadata.get("parentToolUseId")]

    def errors(self) -> list[str]:
        return [
            r.content[:300]
            for r in self.rows
            if r.kind == "tool_result"
            and r.metadata.get("isError")
            and not r.metadata.get("parentToolUseId")
        ]

    def texts(self) -> list[str]:
        return [r.content for r in self.rows if r.role == "assistant" and r.kind == "text"]


class LiveApp:
    def __init__(self, app: Any, client: httpx.AsyncClient, project: Project) -> None:
        self.app, self.client, self.project = app, client, project
        self.pid = project.id
        self.db = app.state.db

    @property
    def design(self) -> Path:
        return self.project.dir / "docs" / "design"

    def url(self, rest: str) -> str:
        return f"/api/projects/{self.pid}{rest}"

    async def engine(self) -> RunEngine:
        engine: RunEngine = await self.app.state.agent_runtime.ensure(self.project)
        return engine

    async def open_project(self) -> None:
        """What the project page does first: start the project's KiCad session."""
        r = await self.client.post(self.url("/open"))
        assert r.status_code == 200, r.text

    async def create(self, kind: str) -> str:
        r = await self.client.post(self.url("/conversations"), json={"type": kind})
        assert r.status_code == 201, r.text
        return str(r.json()["id"])

    async def send(self, cid: str, text: str, timeout: float = 900) -> None:
        engine = await self.engine()
        await engine.submit_ex(cid, uuid.uuid4().hex, text, [])
        await self.idle(timeout)

    async def idle(self, timeout: float = 900) -> None:
        engine = await self.engine()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            queued = await MessageRepo(self.db).queued_user_rows(self.pid)
            if not engine.is_busy and not queued:
                await asyncio.sleep(0.2)
                if not engine.is_busy:
                    return
            if engine.rate_limited_until is not None:
                raise AssertionError(f"rate limited until {engine.rate_limited_until}")
            if loop.time() > deadline:
                raise AssertionError("engine did not become idle")
            await asyncio.sleep(0.5)

    async def turn(self, cid: str) -> Turn:
        rows, _ = await MessageRepo(self.db).page(self.pid, cid, limit=2000)
        return Turn(sorted(rows, key=lambda r: r.id))

    async def usage(self, cid: str) -> dict[str, Any]:
        totals = await ConversationRepo(self.db).usage_totals(self.pid)
        return dict(totals.get(cid, {}))

    async def open_plan(self) -> dict[str, Any] | None:
        r = await self.client.get(self.url("/plans/open"))
        assert r.status_code == 200, r.text
        data = r.json()
        plan = data.get("plan", data) if isinstance(data, dict) else None
        return plan if plan and plan.get("id") else None

    async def doc(self, name: str) -> dict[str, Any]:
        r = await self.client.get(self.url(f"/docs/{name}"))
        assert r.status_code == 200, r.text
        return dict(r.json())

    async def approve(self, name: str) -> dict[str, Any]:
        doc = await self.doc(name)
        r = await self.client.post(
            self.url(f"/docs/{name}/approve"), json={"version": doc["version"]}
        )
        assert r.status_code == 200, r.text
        return dict(r.json())


def app_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """``live_settings`` plus what the app's lifespan needs (a first-start admin)."""
    values: dict[str, Any] = {
        "projects_root": tmp_path / "projects",
        "admin_password": SecretStr("live-test-password"),
        "cookie_secure": False,
        "frontend_dist": tmp_path / "no-frontend",
        **overrides,
    }
    values["projects_root"].mkdir(parents=True, exist_ok=True)
    return live_settings(tmp_path).model_copy(update=values)


@asynccontextmanager
async def live_app(settings: Settings) -> AsyncIterator[LiveApp]:
    app = create_app(settings)
    app.dependency_overrides[require_user] = lambda: User(id=1, username="admin")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            [project] = discover_projects(settings.projects_root)
            yield LiveApp(app, client, project)


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
