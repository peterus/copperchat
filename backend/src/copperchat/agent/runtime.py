"""One RunEngine per project, created when the project is opened or first subscribed."""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from copperchat.agent.attachments import resolve_attachment
from copperchat.agent.broadcaster import Broadcaster
from copperchat.agent.engine import BackendFactory, RunEngine, RunHooks
from copperchat.agent.events import AgentBackend
from copperchat.agent.fake import FakeAgent
from copperchat.agent.sdk_backend import SdkAgentBackend
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.db.repos.agent_state import RateLimitRepo
from copperchat.db.repos.messages import MessageRepo
from copperchat.errors import CopperChatError
from copperchat.kicad_session import KicadSessionManager
from copperchat.projects import Project, get_project
from copperchat.tools import ToolContext, ToolSpec

if TYPE_CHECKING:
    from copperchat.editing.service import EditingService

log = logging.getLogger(__name__)
HooksFactory = Callable[[Project, RunEngine], RunHooks]


def default_backend_factory(
    settings: Settings, specs: Sequence[ToolSpec], db: Database | None = None
) -> BackendFactory:
    """FakeAgent when ``dev_fake_agent`` (no model credential needed), else the SDK backend
    (``db``: skills + attachments, X5)."""

    def make(
        ctx_factory: Callable[[], ToolContext], turn_context: Callable[[], str | None]
    ) -> AgentBackend:
        if settings.dev_fake_agent:
            return FakeAgent(
                specs=specs, ctx_factory=ctx_factory, settings=settings, turn_context=turn_context
            )
        return SdkAgentBackend(
            settings=settings,
            specs=specs,
            ctx_factory=ctx_factory,
            db=db,
            turn_context=turn_context,
        )

    return make


def _no_hooks(project: Project, engine: RunEngine) -> RunHooks:
    return RunHooks()  # Phase 7 replaces hooks_factory


class AgentRuntime:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        broadcaster: Broadcaster,
        sessions: KicadSessionManager,
        backend_factory: BackendFactory | None = None,
        hooks_factory: HooksFactory | None = None,
    ) -> None:
        # Imported here: tool providers may import copperchat.agent helpers (later phases);
        # a module-level import would risk a cycle.
        from copperchat.tools import registry

        self.settings = settings
        self.db = db
        self.broadcaster = broadcaster
        self.sessions = sessions
        self.backend_factory: BackendFactory = backend_factory or default_backend_factory(
            settings, registry.all_specs(), db
        )
        self.hooks_factory: HooksFactory = hooks_factory or _no_hooks
        # SP2 editing service (install_editing); every engine passes it into ToolContext.
        self.editing: EditingService | None = None
        self._engines: dict[str, RunEngine] = {}
        self._lock = asyncio.Lock()

    def get(self, project_id: str) -> RunEngine | None:
        return self._engines.get(project_id)

    async def ensure(self, project: Project) -> RunEngine:
        engine = self._engines.get(project.id)
        if engine is not None:
            return engine
        async with self._lock:
            engine = self._engines.get(project.id)
            if engine is not None:
                return engine
            engine = RunEngine(
                project=project,
                settings=self.settings,
                db=self.db,
                broadcaster=self.broadcaster,
                sessions=self.sessions,
                backend_factory=self.backend_factory,
            )
            engine.hooks = self.hooks_factory(project, engine)
            engine.editing = self.editing
            engine.attachment_resolver = functools.partial(resolve_attachment, self.db, project.id)
            await engine.start()
            self._engines[project.id] = engine
            return engine

    async def resume_pending(self) -> list[str]:
        """Start engines for projects with work left over from a previous process: a
        stored rate limit (the engine re-arms its resume timer) or queued user messages
        (the engine runs them). No client and no KiCad session are needed. Returns the
        project ids whose engine was started; unknown projects are skipped."""
        pending = set(await RateLimitRepo(self.db).parked_projects())
        pending.update(await MessageRepo(self.db).projects_with_queued())
        started: list[str] = []
        for project_id in sorted(pending):
            try:
                project = get_project(self.settings.projects_root, project_id)
            except CopperChatError as exc:
                log.warning("not resuming %s: %s", project_id, exc)
                continue
            try:
                await self.ensure(project)
            except Exception:
                log.exception("resuming %s failed", project_id)  # never block startup
                continue
            started.append(project_id)
        return started

    async def close_all(self) -> None:
        engines, self._engines = list(self._engines.values()), {}
        for engine in engines:
            await engine.close()
