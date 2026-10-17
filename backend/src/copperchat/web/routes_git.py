from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from copperchat.git_tools.specs import git_status_data
from copperchat.projects import get_project
from copperchat.web.auth import CurrentUser

router = APIRouter(prefix="/api/projects", tags=["git"])


@router.get("/{project_id}/git/status")
async def git_status(project_id: str, request: Request, _: CurrentUser) -> dict[str, Any]:
    project = get_project(request.app.state.settings.projects_root, project_id)
    return await git_status_data(project.dir)
