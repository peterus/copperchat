"""Serves the built PWA at ``/`` with an SPA fallback (index X6).

Any GET that is not under ``/api`` and matches no file in ``frontend_dist`` returns
``index.html``, so client routes (``/login``, ``/p/<projectId>`` — the push click target)
survive a reload or a direct navigation. Unknown ``/api`` paths stay JSON 404s.
``index.html``, ``sw.js`` and ``manifest.webmanifest`` are ``no-cache`` (a new release is
picked up on the next load); Vite's content-hashed ``assets/`` are immutable.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response
from starlette.types import Scope

NO_CACHE = frozenset({"index.html", "sw.js", "manifest.webmanifest"})


class SpaStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        if path == "api" or path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            response = None
        if response is None or response.status_code == 404:
            path = "index.html"
            response = await super().get_response(path, scope)
        name = "index.html" if path in ("", ".") else path
        if name in NO_CACHE or name.endswith("/index.html"):
            response.headers["Cache-Control"] = "no-cache"
        elif name.startswith("assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def mount_frontend(app: FastAPI, dist: Path) -> None:
    """Serve the built PWA at / when present (must be mounted after every router)."""
    if (dist / "index.html").is_file():
        app.mount("/", SpaStaticFiles(directory=dist, html=True), name="frontend")
