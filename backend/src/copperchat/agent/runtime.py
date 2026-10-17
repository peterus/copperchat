"""One RunEngine per project, created when the project is opened or first subscribed."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

from copperchat.agent.broadcaster import Broadcaster
from copperchat.agent.engine import BackendFactory, RunEngine, RunHooks
from copperchat.agent.events import AgentBackend
from copperchat.agent.fake import FakeAgent
from copperchat.agent.sdk_backend import SdkAgentBackend
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.kicad_session import KicadSessionManager
from copperchat.projects import Project
from copperchat.tools import ToolContext, ToolSpec

HooksFactory = Callable[[Project, RunEngine], RunHooks]


def default_backend_factory(settings: Settings, specs: Sequence[ToolSpec]) -> BackendFactory:
    """FakeAgent when ``dev_fake_agent`` (no model credential needed), else the SDK backend."""

    def make(ctx_factory: Callable[[], ToolContext]) -> AgentBackend:
        if settings.dev_fake_agent:
            return FakeAgent(specs=specs, ctx_factory=ctx_factory)
        return SdkAgentBackend(settings=settings, specs=specs, ctx_factory=ctx_factory)

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
            settings, registry.all_specs()
        )
        self.hooks_factory: HooksFactory = hooks_factory or _no_hooks
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
            await engine.start()
            self._engines[project.id] = engine
            return engine

    async def close_all(self) -> None:
        engines, self._engines = list(self._engines.values()), {}
        for engine in engines:
            await engine.close()
