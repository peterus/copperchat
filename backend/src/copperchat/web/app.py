from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from copperchat.agent.broadcaster import Broadcaster
from copperchat.agent.runtime import AgentRuntime
from copperchat.config import Settings, check_model_credentials
from copperchat.db import Database
from copperchat.editing import install_editing
from copperchat.errors import CopperChatError, StructuredToolError
from copperchat.history.service import install_history
from copperchat.kicad_session import ClientFactory, KicadSessionManager, ServerLauncher
from copperchat.library.prefs import seed_user_part_preferences
from copperchat.plans import install_plans
from copperchat.projects import get_project
from copperchat.push.dispatcher import PushDispatcher
from copperchat.push.notifier import PushNotifier
from copperchat.push.store import PushStore
from copperchat.push.vapid import load_or_create_vapid
from copperchat.push.visibility import VisibilityRegistry
from copperchat.web import (
    auth,
    health,
    routes_conversations,
    routes_editing,
    routes_git,
    routes_history,
    routes_messages,
    routes_parts,
    routes_plans,
    routes_projects,
    routes_push,
    routes_skills,
    routes_uploads,
    routes_views,
    ws,
)
from copperchat.web.routes_push import PushServices
from copperchat.web.security import SecurityHeadersMiddleware
from copperchat.web.static import mount_frontend

log = logging.getLogger(__name__)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(CopperChatError)
    async def _handle(_: Request, exc: CopperChatError) -> JSONResponse:
        body: dict[str, object] = {"code": exc.code, "message": exc.message}
        if isinstance(exc, StructuredToolError):
            body["details"] = exc.to_dict()
        return JSONResponse(status_code=exc.http_status, content={"error": body})


def install_push(app: FastAPI) -> PushServices:
    """Web Push (Phase 9): VAPID keys, subscriptions, visibility registry and the notifier,
    which listens to every frame the agent runtime broadcasts (X17: needs the runtime)."""
    settings: Settings = app.state.settings
    runtime: AgentRuntime = app.state.agent_runtime
    keys = load_or_create_vapid(settings.data_dir)
    store = PushStore(app.state.db)
    visibility = VisibilityRegistry()
    dispatcher = PushDispatcher(store, keys, settings.vapid_subject)

    def project_name(project_id: str) -> str:
        try:
            return get_project(settings.projects_root, project_id).name
        except CopperChatError:
            return "CopperChat"

    notifier = PushNotifier(app.state.db, dispatcher, visibility, project_name)
    services = PushServices(keys, store, visibility, dispatcher, notifier)
    app.state.push = services
    runtime.broadcaster.add_listener(notifier.on_frame)
    return services


def create_app(
    settings: Settings,
    *,
    launcher: ServerLauncher | None = None,
    client_factory: ClientFactory | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        check_model_credentials(settings)
        if (warning := auth.startup_cookie_warning(settings)) is not None:
            auth.log.warning(warning)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            seed_user_part_preferences(settings)  # owner default: passives in 0805 (E3)
        except Exception as exc:  # a seed problem must never stop the app
            log.warning("could not seed the user part preferences: %s", exc)
        db = await Database.open(settings.data_dir / "copperchat.db")
        sessions = KicadSessionManager(settings, launcher, client_factory=client_factory)
        runtime: AgentRuntime | None = None
        push: PushServices | None = None
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
            # X17: services that need app.state.agent_runtime are set up here (history,
            # push), after the runtime and before yield. install_history sets
            # runtime.hooks_factory, so it must run before resume_pending() creates
            # engines: an engine keeps the hooks it was created with.
            install_history(app)
            install_editing(app)  # after history: chains its hooks after HistoryHooks
            plans = install_plans(app)  # PlanGate replaces the editing gate (E8b)
            await plans.pause_all_running()  # a restart leaves no plan running
            push = install_push(app)
            # parked / queued work survives a restart
            for project_id in await runtime.resume_pending():
                engine = runtime.get(project_id)
                if engine is not None and engine.rate_limited_until is not None:
                    push.notifier.schedule_reopen(project_id, engine.rate_limited_until)
            yield
        finally:
            if push is not None:
                await push.notifier.close()
            if runtime is not None:
                await runtime.close_all()
            await sessions.close_all()
            await db.close()

    app = FastAPI(title="CopperChat", lifespan=lifespan)
    install_error_handlers(app)
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(auth.router)
    app.include_router(health.router)
    app.include_router(routes_projects.router)
    app.include_router(routes_views.router)
    app.include_router(routes_messages.router)
    app.include_router(routes_conversations.router)
    app.include_router(routes_history.router)
    app.include_router(routes_git.router)
    app.include_router(routes_skills.router)
    app.include_router(routes_parts.router)
    app.include_router(routes_uploads.router)
    app.include_router(routes_push.router)
    app.include_router(routes_editing.router)
    app.include_router(routes_plans.router)
    app.include_router(ws.router)
    mount_frontend(app, settings.frontend_dist)  # must stay last: it mounts "/"
    return app
