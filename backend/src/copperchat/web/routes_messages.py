"""Chat history paging and tool image download."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse

from copperchat.agent.engine import RunEngine
from copperchat.agent.protocol import MessageDTO
from copperchat.db.repos.messages import MessageRepo
from copperchat.errors import CopperChatError
from copperchat.projects import agent_dir, get_project
from copperchat.web.auth import CurrentUser

router = APIRouter(prefix="/api/projects", tags=["messages"])
# Names the engine writes (uuid4 hex + extension); anything else is never served.
_IMAGE_NAME = re.compile(r"^[0-9a-f]{32}\.(png|jpg|webp|bin)$")


class ImageNotFound(CopperChatError):
    code = "not_found"
    http_status = 404


@router.get("/{project_id}/messages")
async def list_messages(
    project_id: str,
    request: Request,
    _: CurrentUser,
    limit: int = Query(50, ge=1, le=200),
    before: int | None = Query(None, ge=1),
) -> dict[str, Any]:
    state = request.app.state
    project = get_project(state.settings.projects_root, project_id)
    rows, has_more = await MessageRepo(state.db).page(project.id, limit=limit, before=before)
    # The engine (if any) overlays the live content of a row that is still streaming.
    engine: RunEngine | None = state.agent_runtime.get(project.id)
    dtos = [engine.to_dto(r) if engine else MessageDTO.model_validate(r.to_wire()) for r in rows]
    return {
        "messages": [d.model_dump(mode="json", by_alias=True) for d in dtos],
        "hasMore": has_more,
    }


@router.get("/{project_id}/images/{name}")
async def get_image(project_id: str, name: str, request: Request, _: CurrentUser) -> FileResponse:
    if not _IMAGE_NAME.fullmatch(name):
        raise ImageNotFound("no such image")
    project = get_project(request.app.state.settings.projects_root, project_id)
    root = (agent_dir(project) / "images").resolve()
    path = (root / name).resolve()  # a symlink pointing elsewhere resolves outside root
    if path.parent != root or not path.is_file():
        raise ImageNotFound("no such image")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})
