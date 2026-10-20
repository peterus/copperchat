"""Conversations ("sessions") of a project and their message pages (spec §6.1).

Every ``cid`` is checked against the project: a conversation of another project is a 404
(``conversation_not_found``). Close and reopen (sessions C5, spec §10) live in
``copperchat.sessions``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import AfterValidator, BaseModel, ConfigDict, StrictBool, StringConstraints

from copperchat.agent.engine import RunEngine, conversation_dto, publish_conversation
from copperchat.agent.protocol import MessageDTO
from copperchat.db.repos.conversations import (
    TITLE_MAX,
    ConversationRepo,
    ConversationRow,
    validate_title,
)
from copperchat.db.repos.messages import MessageRepo
from copperchat.errors import ConversationNotFound, CopperChatError, InvalidConversation
from copperchat.projects import Project, get_project
from copperchat.web.auth import CurrentUser

router = APIRouter(prefix="/api/projects", tags=["conversations"])


def _title(value: str) -> str:
    try:
        return validate_title(value)
    except InvalidConversation as exc:
        raise ValueError(exc.message) from exc


# The repo's rule (a single line of visible plain text), as a 422 validation error.
Title = Annotated[str, StringConstraints(max_length=TITLE_MAX), AfterValidator(_title)]


class SessionTypeUnavailable(CopperChatError):
    code = "session_type_unavailable"
    http_status = 400


class CreateConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["free", "requirements", "schematic", "board"]
    title: Title | None = None


class PatchConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Title | None = None
    archived: StrictBool | None = None


def _project(request: Request, project_id: str) -> Project:
    return get_project(request.app.state.settings.projects_root, project_id)


def _repo(request: Request) -> ConversationRepo:
    return ConversationRepo(request.app.state.db)


async def _owned(request: Request, project: Project, cid: str) -> ConversationRow:
    if len(cid) != 32:
        raise ConversationNotFound("no such session in this project")
    return await _repo(request).get_for_project(project.id, cid)


async def _published(request: Request, row: ConversationRow) -> dict[str, Any]:
    broadcaster = request.app.state.agent_runtime.broadcaster
    dto = await publish_conversation(broadcaster, _repo(request), row)
    return dto.model_dump(mode="json", by_alias=True)


@router.get("/{project_id}/conversations")
async def list_conversations(
    project_id: str,
    request: Request,
    _: CurrentUser,
    archived: int = Query(0, ge=0, le=1),
) -> list[dict[str, Any]]:
    project = _project(request, project_id)
    repo = _repo(request)
    usage = await repo.usage_totals(project.id)
    return [
        dict(row.to_wire(usage.get(row.id)))
        for row in await repo.list(project.id, archived=bool(archived))
    ]


@router.post("/{project_id}/conversations", status_code=201)
async def create_conversation(
    project_id: str, body: CreateConversation, request: Request, _: CurrentUser
) -> dict[str, Any]:
    project = _project(request, project_id)
    if body.type == "board":
        raise SessionTypeUnavailable("Board sessions come with sub-project 4")
    row = await _repo(request).create(project.id, body.type, body.title)
    return await _published(request, row)


@router.patch("/{project_id}/conversations/{cid}")
async def patch_conversation(
    project_id: str, cid: str, body: PatchConversation, request: Request, _: CurrentUser
) -> dict[str, Any]:
    project = _project(request, project_id)
    row = await _owned(request, project, cid)
    if body.title is None and body.archived is None:
        return (await conversation_dto(_repo(request), row)).model_dump(mode="json", by_alias=True)
    row = await _repo(request).update(row.id, title=body.title, archived=body.archived)
    return await _published(request, row)


@router.get("/{project_id}/conversations/{cid}/messages")
async def list_messages(
    project_id: str,
    cid: str,
    request: Request,
    _: CurrentUser,
    limit: int = Query(50, ge=1, le=200),
    before: int | None = Query(None, ge=1),
) -> dict[str, Any]:
    state = request.app.state
    project = _project(request, project_id)
    conv = await _owned(request, project, cid)
    rows, has_more = await MessageRepo(state.db).page(
        project.id, conv.id, limit=limit, before=before
    )
    # The engine (if any) overlays the live content of a row that is still streaming.
    engine: RunEngine | None = state.agent_runtime.get(project.id)
    dtos = [engine.to_dto(r) if engine else MessageDTO.model_validate(r.to_wire()) for r in rows]
    return {
        "messages": [d.model_dump(mode="json", by_alias=True) for d in dtos],
        "hasMore": has_more,
    }


@router.post("/{project_id}/conversations/{cid}/close", status_code=202)
async def close_conversation(
    project_id: str, cid: str, request: Request, _: CurrentUser
) -> dict[str, Any]:
    """``closing`` + the server-authored handover message (409 unless open, 409
    ``plan_open`` while the session owns an open plan)."""
    project = _project(request, project_id)
    conv = await _owned(request, project, cid)
    row = await request.app.state.conversation_service.close(project, conv.id)
    return (await conversation_dto(_repo(request), row)).model_dump(mode="json", by_alias=True)


@router.post("/{project_id}/conversations/{cid}/reopen")
async def reopen_conversation(
    project_id: str, cid: str, request: Request, _: CurrentUser
) -> dict[str, Any]:
    project = _project(request, project_id)
    conv = await _owned(request, project, cid)
    row = await request.app.state.conversation_service.reopen(project, conv.id)
    return (await conversation_dto(_repo(request), row)).model_dump(mode="json", by_alias=True)
