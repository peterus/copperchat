from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from copperchat.agent.broadcaster import Broadcaster
from copperchat.agent.runtime import AgentRuntime
from copperchat.config import Settings, check_model_credentials
from copperchat.db import Database
from copperchat.errors import CopperChatError, StructuredToolError
from copperchat.history.service import install_history
from copperchat.kicad_session import ClientFactory, KicadSessionManager, ServerLauncher
from copperchat.web import (
    auth,
    health,
    routes_git,
    routes_history,
    routes_messages,
    routes_projects,
    routes_views,
    ws,
)
from copperchat.web.static import mount_frontend


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(CopperChatError)
    async def _handle(_: Request, exc: CopperChatError) -> JSONResponse:
        body: dict[str, object] = {"code": exc.code, "message": exc.message}
        if isinstance(exc, StructuredToolError):
            body["details"] = exc.to_dict()
        return JSONResponse(status_code=exc.http_status, content={"error": body})


def create_app(
    settings: Settings,
    *,
    launcher: ServerLauncher | None = None,
    client_factory: ClientFactory | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        check_model_credentials(settings)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db = await Database.open(settings.data_dir / "copperchat.db")
        sessions = KicadSessionManager(settings, launcher, client_factory=client_factory)
        runtime: AgentRuntime | None = None
        try:
            await auth.ensure_admin(db, settings)
            app.state.settings = settings
            app.state.db = db
            app.state.sessions = sessions
            app.state.serializer = auth.make_serializer(
                auth.load_or_create_session_secret(settings)
            )
            app.state.login_limiter = auth.LoginRateLimiter(
                settings.login_max_failures, settings.login_window_s
            )
            runtime = AgentRuntime(
                settings=settings, db=db, broadcaster=Broadcaster(), sessions=sessions
            )
            app.state.agent_runtime = runtime
            # X17: services that need app.state.agent_runtime are set up here (Phase 9
            # push too), after the runtime and before yield. install_history sets
            # runtime.hooks_factory, so it must run before resume_pending() creates
            # engines: an engine keeps the hooks it was created with.
            install_history(app)
            await runtime.resume_pending()  # parked / queued work survives a restart
            yield
        finally:
            if runtime is not None:
                await runtime.close_all()
            await sessions.close_all()
            await db.close()

    app = FastAPI(title="CopperChat", lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(auth.router)
    app.include_router(health.router)
    app.include_router(routes_projects.router)
    app.include_router(routes_views.router)
    app.include_router(routes_messages.router)
    app.include_router(routes_history.router)
    app.include_router(routes_git.router)
    app.include_router(ws.router)
    mount_frontend(app, settings.frontend_dist)  # must stay last: it mounts "/"
    return app
