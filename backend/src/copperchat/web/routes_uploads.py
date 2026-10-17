"""Chat attachment uploads (composer) and their files (thumbnails)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from copperchat.db import utcnow_iso
from copperchat.db.repos.uploads import UploadRow, get_upload, insert_upload
from copperchat.projects import get_project
from copperchat.uploads import MAX_UPLOAD_BYTES, UPLOAD_ID_RE, UploadRejected, store_upload
from copperchat.web.auth import CurrentUser

router = APIRouter(prefix="/api/projects/{project_id}/uploads", tags=["uploads"])


@router.post("", status_code=201)
async def upload(
    request: Request, project_id: str, file: UploadFile, _: CurrentUser
) -> dict[str, str]:
    project = get_project(request.app.state.settings.projects_root, project_id)
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    try:
        # Decoding/re-encoding an image is CPU-bound: keep it off the event loop.
        stored = await asyncio.to_thread(store_upload, project, data, file.filename)
    except UploadRejected as exc:
        raise HTTPException(exc.status, exc.message) from exc
    await insert_upload(
        request.app.state.db,
        UploadRow(
            id=stored.id,
            project_id=project.id,
            kind=stored.kind,
            name=stored.name,
            rel_path=stored.rel_path,
            mime=stored.mime,
            size=stored.size,
            created_at=utcnow_iso(),
        ),
    )
    return {"id": stored.id, "kind": stored.kind, "name": stored.name}


@router.get("/{upload_id}")
async def get_file(
    request: Request, project_id: str, upload_id: str, _: CurrentUser
) -> FileResponse:
    project = get_project(request.app.state.settings.projects_root, project_id)
    row = (
        await get_upload(request.app.state.db, project.id, upload_id)
        if UPLOAD_ID_RE.fullmatch(upload_id)
        else None
    )
    path = None if row is None else project.dir / row.rel_path
    if (
        row is None
        or path is None
        or path.is_symlink()
        or not path.is_file()
        or not path.resolve().is_relative_to(project.dir.resolve())
    ):
        raise HTTPException(404, "upload not found")
    disposition = "inline" if row.kind == "image" else "attachment"
    return FileResponse(
        path,
        media_type=row.mime,
        headers={
            "Content-Disposition": f'{disposition}; filename="{row.name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
