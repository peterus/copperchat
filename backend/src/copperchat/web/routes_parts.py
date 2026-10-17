"""Parts page API: docs/parts/<MPN>/ notes and datasheets."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from copperchat import parts
from copperchat.projects import Project, get_project
from copperchat.web.auth import CurrentUser

router = APIRouter(prefix="/api/projects/{project_id}/parts", tags=["parts"])


class NotesBody(BaseModel):
    content: str = Field(max_length=parts.MAX_NOTES_BYTES)


def _project(request: Request, project_id: str) -> Project:
    return get_project(request.app.state.settings.projects_root, project_id)


def _mpn(mpn: str) -> str:
    try:
        return parts.mpn_dirname(mpn)
    except parts.InvalidMpn as exc:
        raise HTTPException(400, exc.message) from exc


@router.get("")
async def list_parts(request: Request, project_id: str, _: CurrentUser) -> list[dict[str, Any]]:
    return [
        {
            "mpn": p.mpn,
            "hasNotes": p.has_notes,
            "hasDatasheet": p.has_datasheet,
            "userEdited": p.user_edited,
            "updatedAt": p.updated_at,
        }
        for p in parts.list_parts(_project(request, project_id))
    ]


@router.get("/{mpn}/notes")
async def get_notes(request: Request, project_id: str, mpn: str, _: CurrentUser) -> dict[str, Any]:
    project, name = _project(request, project_id), _mpn(mpn)
    try:
        content = parts.read_notes(project, name)
        edited = parts.notes_user_edited(parts.part_dir(project, name) / "notes.md")
    except parts.InvalidMpn as exc:
        raise HTTPException(400, exc.message) from exc
    return {"mpn": name, "content": content, "userEdited": edited}


@router.put("/{mpn}/notes", status_code=204)
async def put_notes(
    request: Request, project_id: str, mpn: str, body: NotesBody, _: CurrentUser
) -> Response:
    project, name = _project(request, project_id), _mpn(mpn)
    try:
        parts.write_user_notes(project, name, body.content)
    except parts.InvalidMpn as exc:
        raise HTTPException(400, exc.message) from exc
    return Response(status_code=204)


@router.get("/{mpn}/datasheet")
async def datasheet(request: Request, project_id: str, mpn: str, _: CurrentUser) -> FileResponse:
    path = parts.datasheet_path(_project(request, project_id), _mpn(mpn))
    if path is None:
        raise HTTPException(404, "no datasheet")
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{path.parent.name}.pdf"',
            "X-Content-Type-Options": "nosniff",
        },
    )
