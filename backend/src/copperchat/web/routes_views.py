from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, cast

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse

from copperchat.errors import CopperChatError
from copperchat.kicad_session import DocKind, KicadSession, KicadSessionManager
from copperchat.projects import agent_dir, get_project
from copperchat.views.render import render_view
from copperchat.views.snapshot import resolve_view_file, snapshot_path
from copperchat.web.auth import CurrentUser

router = APIRouter(prefix="/api/projects", tags=["views"])
_RENDER = re.compile(r"^[0-9a-f]{32}\.png$")
_DOCS = ("sch", "pcb")
_THEMES = ("agent", "light", "dark")
_MODES = ("agent", "kicad")


class BadViewRequest(CopperChatError):
    code = "bad_view_request"
    http_status = 400


class ViewNotFound(CopperChatError):
    code = "not_found"
    http_status = 404


class ProjectNotOpen(CopperChatError):
    code = "project_not_open"
    http_status = 409


def _session_for(request: Request, project_id: str) -> KicadSession:
    project = get_project(request.app.state.settings.projects_root, project_id)
    sessions: KicadSessionManager = request.app.state.sessions
    current = sessions.current()
    if current is None or current.project.id != project.id:
        raise ProjectNotOpen("project is not open - POST /api/projects/{id}/open first")
    return current


def _etag(session: KicadSession, path: Path) -> str:
    # The view version (X2) restarts at 0 with every session; the file's mtime
    # keeps a reopened project's snapshot from matching a stale cached copy.
    return f'"{session.view_version}.{path.stat().st_mtime_ns:x}"'


def _serve_snapshot(request: Request, session: KicadSession, path: Path) -> Response:
    etag = _etag(session, path)
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type="application/octet-stream", headers=headers)


@router.get("/{project_id}/view/files/{name:path}")
async def get_view_file(project_id: str, name: str, request: Request, _: CurrentUser) -> Response:
    """KiCanvas source files under their real names, root sheet and sub-sheets (X3)."""
    session = _session_for(request, project_id)
    path = resolve_view_file(session.project, name)
    if path is None:
        raise ViewNotFound("no such view file")
    return _serve_snapshot(request, session, path)


@router.get("/{project_id}/view/{doc}")
async def get_view(project_id: str, doc: str, request: Request, _: CurrentUser) -> Response:
    if doc not in _DOCS:
        raise BadViewRequest("doc must be 'sch' or 'pcb'")
    session = _session_for(request, project_id)
    path = snapshot_path(session.project, cast(DocKind, doc))
    if not path.is_file():
        raise ViewNotFound(f"the project has no {doc} view")
    return _serve_snapshot(request, session, path)


@router.get("/{project_id}/renders/{file}")
async def get_render(project_id: str, file: str, request: Request, _: CurrentUser) -> Response:
    if not _RENDER.fullmatch(file):
        raise BadViewRequest("invalid render file name")
    project = get_project(request.app.state.settings.projects_root, project_id)
    root = (agent_dir(project) / "renders").resolve()
    path = (root / file).resolve()
    if path.parent != root or not path.is_file():
        raise ViewNotFound("no such render")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private"})


@router.get("/{project_id}/agent-view")
async def get_agent_view(
    project_id: str,
    request: Request,
    _: CurrentUser,
    doc: str = Query("pcb"),
    mode: str = Query("agent"),
    theme: str = Query("agent"),
    layers: str | None = Query(None),
) -> Response:
    if doc not in _DOCS or mode not in _MODES or theme not in _THEMES:
        raise BadViewRequest("doc must be sch|pcb, mode agent|kicad, theme agent|light|dark")
    session = _session_for(request, project_id)
    layer_list = [x.strip() for x in (layers or "").split(",") if x.strip()] or None
    img = await render_view(
        session,
        cast(DocKind, doc),
        cast(Literal["agent", "kicad"], mode),
        layers=layer_list,
        theme=cast(Literal["agent", "light", "dark"], theme),
    )
    return Response(content=img.data, media_type="image/png", headers={"Cache-Control": "no-store"})
