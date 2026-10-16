from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from copperchat.db.repos.projects import touch_opened, upsert_projects
from copperchat.kicad_session import KicadSessionManager
from copperchat.projects import discover_projects, get_project
from copperchat.web.auth import CurrentUser

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("")
async def list_projects(request: Request, _: CurrentUser) -> list[dict[str, Any]]:
    sessions: KicadSessionManager = request.app.state.sessions
    projects = discover_projects(request.app.state.settings.projects_root)
    await upsert_projects(request.app.state.db, projects)
    return [
        {
            "id": p.id,
            "name": p.name,
            "relPath": p.rel_path,
            "hasSch": p.sch_file is not None,
            "hasPcb": p.pcb_file is not None,
            "locked": sessions.lock_status(p).locked,
        }
        for p in projects
    ]


@router.post("/{project_id}/open")
async def open_project(project_id: str, request: Request, _: CurrentUser) -> dict[str, Any]:
    state = request.app.state
    project = get_project(state.settings.projects_root, project_id)
    sessions: KicadSessionManager = state.sessions
    session = await sessions.open(project)
    await touch_opened(state.db, project.id)
    version: str = await session.call(lambda h: str(h.kicad.get_version().full_version))
    return {
        "id": project.id,
        "readOnly": session.read_only,
        "lockFiles": sorted(p.name for p in session.lock_files),
        "kicadVersion": version,
    }
