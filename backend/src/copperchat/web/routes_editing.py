"""Editing settings API (E-C12): GET|PUT /api/projects/{id}/settings/editing."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from copperchat.db.repos.editing_settings import EditingSettings, EditingSettingsRepo
from copperchat.projects import get_project
from copperchat.web.auth import CurrentUser

router = APIRouter(prefix="/api", tags=["editing"])


class EditingSettingsBody(BaseModel):
    """Partial update; values are validated by the repo (400 on invalid values)."""

    model_config = ConfigDict(extra="forbid")

    planPolicy: Any = None
    changeBudget: Any = None
    annotationBlock: Any = None
    showAgentView: Any = None


_FIELD_MAP = {
    "planPolicy": "plan_policy",
    "changeBudget": "change_budget",
    "annotationBlock": "annotation_block",
    "showAgentView": "show_agent_view",
}


def _dto(s: EditingSettings) -> dict[str, Any]:
    return {
        "planPolicy": s.plan_policy,
        "changeBudget": s.change_budget,
        "annotationBlock": s.annotation_block,
        "showAgentView": s.show_agent_view,
    }


def _repo(request: Request, project_id: str) -> EditingSettingsRepo:
    get_project(request.app.state.settings.projects_root, project_id)
    return EditingSettingsRepo(request.app.state.db)


@router.get("/projects/{project_id}/settings/editing")
async def get_editing_settings(project_id: str, request: Request, _: CurrentUser) -> dict[str, Any]:
    return _dto(await _repo(request, project_id).get(project_id))


@router.put("/projects/{project_id}/settings/editing")
async def put_editing_settings(
    project_id: str, body: EditingSettingsBody, request: Request, _: CurrentUser
) -> dict[str, Any]:
    repo = _repo(request, project_id)
    values = {_FIELD_MAP[k]: v for k, v in body.model_dump(exclude_unset=True).items()}
    return _dto(await repo.put(project_id, **values))
