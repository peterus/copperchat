from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from copperchat.db.repos.projects import upsert_projects
from copperchat.projects import discover_projects, get_project, lock_status
from copperchat.web.auth import CurrentUser

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("")
async def list_projects(request: Request, _: CurrentUser) -> list[dict[str, Any]]:
    projects = discover_projects(request.app.state.settings.projects_root)
    await upsert_projects(request.app.state.db, projects)
    return [
        {
            "id": p.id,
            "name": p.name,
            "relPath": p.rel_path,
            "hasSch": p.sch_file is not None,
            "hasPcb": p.pcb_file is not None,
            "locked": lock_status(p).locked,
        }
        for p in projects
    ]


@router.post("/{project_id}/open")
async def open_project(project_id: str, request: Request, _: CurrentUser) -> dict[str, Any]:
    get_project(request.app.state.settings.projects_root, project_id)
    raise HTTPException(status_code=501, detail="opening is implemented in Task 3.5")
