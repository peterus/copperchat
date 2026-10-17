from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from copperchat.history.restore import restore_snapshot
from copperchat.history.service import HistoryService
from copperchat.history.store import SnapshotNotFound
from copperchat.projects import Project, get_project
from copperchat.web.auth import CurrentUser

router = APIRouter(prefix="/api/projects", tags=["history"])


def _ctx(request: Request, project_id: str) -> tuple[HistoryService, Project]:
    project = get_project(request.app.state.settings.projects_root, project_id)
    return request.app.state.history, project


@router.get("/{project_id}/history")
async def list_history(project_id: str, request: Request, _: CurrentUser) -> list[dict[str, Any]]:
    svc, project = _ctx(request, project_id)
    return [
        {
            "id": s.id,
            "runId": s.run_id,
            "createdAt": s.created_at,
            "label": s.label,
            "files": s.files,
            "previewUrl": f"/api/projects/{project.id}/history/{s.id}/preview"
            if s.has_preview
            else None,
        }
        for s in await svc.store(project).list()
    ]


@router.get("/{project_id}/history/{snapshot_id}/preview")
async def preview(
    project_id: str, snapshot_id: str, request: Request, _: CurrentUser
) -> FileResponse:
    svc, project = _ctx(request, project_id)
    store = svc.store(project)
    snap = await store.get(snapshot_id)  # InvalidSnapshotId 400 / SnapshotNotFound 404
    path = store.snapshot_dir(snap.id) / "preview.png"
    if not path.is_file():
        raise SnapshotNotFound("no preview for this snapshot")
    # Snapshots never change once written.
    headers = {"Cache-Control": "private, max-age=31536000, immutable"}
    return FileResponse(path, media_type="image/png", headers=headers)


@router.post("/{project_id}/history/{snapshot_id}/restore")
async def restore(
    project_id: str, snapshot_id: str, request: Request, _: CurrentUser
) -> dict[str, Any]:
    svc, project = _ctx(request, project_id)
    # Errors map via install_error_handlers: 400 invalid id, 404 unknown, 409 read-only
    # or agent busy, 422 corrupt snapshot, 503 KiCad unavailable.
    snap = await restore_snapshot(svc, project, snapshot_id)
    return {"id": snap.id, "files": snap.files}
