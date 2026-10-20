"""Tool image download (message pages are per conversation: routes_conversations)."""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from copperchat.errors import CopperChatError
from copperchat.projects import agent_dir, get_project
from copperchat.web.auth import CurrentUser

router = APIRouter(prefix="/api/projects", tags=["messages"])
# Names the engine writes (uuid4 hex + extension); anything else is never served.
_IMAGE_NAME = re.compile(r"^[0-9a-f]{32}\.(png|jpg|webp|bin)$")


class ImageNotFound(CopperChatError):
    code = "not_found"
    http_status = 404


@router.get("/{project_id}/images/{name}")
async def get_image(project_id: str, name: str, request: Request, _: CurrentUser) -> FileResponse:
    if not _IMAGE_NAME.fullmatch(name):
        raise ImageNotFound("no such image")
    project = get_project(request.app.state.settings.projects_root, project_id)
    root = (agent_dir(project) / "images").resolve()
    path = (root / name).resolve()  # a symlink pointing elsewhere resolves outside root
    if path.parent != root or not path.is_file():
        raise ImageNotFound("no such image")
    return FileResponse(
        path,
        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )
