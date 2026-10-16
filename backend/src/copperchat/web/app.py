from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from copperchat.config import Settings, check_model_credentials
from copperchat.db import Database
from copperchat.errors import CopperChatError, StructuredToolError
from copperchat.web import auth, health
from copperchat.web.static import mount_frontend


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(CopperChatError)
    async def _handle(_: Request, exc: CopperChatError) -> JSONResponse:
        body: dict[str, object] = {"code": exc.code, "message": exc.message}
        if isinstance(exc, StructuredToolError):
            body["details"] = exc.to_dict()
        return JSONResponse(status_code=exc.http_status, content={"error": body})


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        check_model_credentials(settings)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db = await Database.open(settings.data_dir / "copperchat.db")
        try:
            await auth.ensure_admin(db, settings)
            app.state.settings = settings
            app.state.db = db
            app.state.serializer = auth.make_serializer(
                auth.load_or_create_session_secret(settings)
            )
            app.state.login_limiter = auth.LoginRateLimiter(
                settings.login_max_failures, settings.login_window_s
            )
            yield
        finally:
            await db.close()

    app = FastAPI(title="CopperChat", lifespan=lifespan)
    _install_error_handlers(app)
    app.include_router(auth.router)
    app.include_router(health.router)
    mount_frontend(app, settings.frontend_dist)  # must stay last: it mounts "/"
    return app
