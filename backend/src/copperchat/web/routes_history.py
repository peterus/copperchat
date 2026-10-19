from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from copperchat.db.repos.conversations import ConversationRepo
from copperchat.errors import ConversationNotFound
from copperchat.history.restore import restore_snapshot
from copperchat.history.service import HistoryService
from copperchat.history.store import SnapshotNotFound
from copperchat.plans.model import PlanStateError
from copperchat.projects import Project, get_project
from copperchat.web.auth import CurrentUser

router = APIRouter(prefix="/api/projects", tags=["history"])


def _ctx(request: Request, project_id: str) -> tuple[HistoryService, Project]:
    project = get_project(request.app.state.settings.projects_root, project_id)
    return request.app.state.history, project


@router.get("/{project_id}/history")
async def list_history(project_id: str, request: Request, _: CurrentUser) -> list[dict[str, Any]]:
    svc, project = _ctx(request, project_id)
    snaps = await svc.store(project).list()
    # The session of each snapshot's turn (spec §5.4): history is project-wide.
    sessions = await ConversationRepo(request.app.state.db).for_runs(
        project.id, sorted({s.run_id for s in snaps})
    )
    return [
        {
            "id": s.id,
            "runId": s.run_id,
            "conversationId": sessions.get(s.run_id, (None, None))[0],
            "conversationTitle": sessions.get(s.run_id, (None, None))[1],
            "createdAt": s.created_at,
            "label": s.label,
            "files": s.files,
            "kind": s.kind,
            "planStepId": s.plan_step_id,
            "previewUrl": f"/api/projects/{project.id}/history/{s.id}/preview"
            if s.has_preview
            else None,
        }
        for s in snaps
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


# Plans that block a restore, with what the user can do about each (Roll back exists
# only for a paused plan; a running one cannot be aborted until the agent stops).
_RESTORE_HINT = {
    "approved": "The approved plan is waiting to run: abort it on its plan card, or wait "
    "for it to run.",
    "running": "Stop the plan first; then use Roll back to... on its plan card, or abort it.",
    "paused": "Use Roll back to... on the plan card, or abort the plan first.",
}


class RestoreBody(BaseModel):
    """The session the user restored from: it gets the restore notice (spec §6.1)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    conversation_id: str = Field(alias="conversationId", min_length=1, max_length=64)


@router.post("/{project_id}/history/{snapshot_id}/restore")
async def restore(
    project_id: str, snapshot_id: str, body: RestoreBody, request: Request, _: CurrentUser
) -> dict[str, Any]:
    svc, project = _ctx(request, project_id)
    # Ownership first: a session of another project restores nothing (404).
    if len(body.conversation_id) != 32:
        raise ConversationNotFound("no such session in this project")
    conversation = await ConversationRepo(request.app.state.db).get_for_project(
        project.id, body.conversation_id
    )
    plans = getattr(request.app.state, "plans", None)
    if plans is not None:
        # E8b: a plan that has begun owns the design's history; a plain restore would let
        # Resume run later steps on a design without the earlier ones (spec §7.1).
        open_ = await plans.repo.open_status(project.id)
        if open_ is not None and open_[1] in _RESTORE_HINT:
            plan_id, status = open_
            raise PlanStateError(
                "The design cannot be restored while a plan is open",
                f"Plan {plan_id} is {status}",  # format parsed by the frontend (fallback)
                _RESTORE_HINT[status],
                code="plan_open",
                details={"planId": plan_id, "status": status},
            )
    # Errors map via install_error_handlers: 400 invalid id, 404 unknown, 409 read-only,
    # agent busy or open plan, 422 corrupt snapshot, 503 KiCad unavailable.
    snap = await restore_snapshot(svc, project, snapshot_id, conversation_id=conversation.id)
    return {"id": snap.id, "files": snap.files}
