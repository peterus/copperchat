from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from kipy.errors import ApiError

from copperchat.db.repos.projects import touch_opened, upsert_projects
from copperchat.errors import CopperChatError
from copperchat.kicad_session import KicadSessionManager, KicadUnavailable
from copperchat.projects import discover_projects, get_project
from copperchat.views.snapshot import write_view_snapshots
from copperchat.web.auth import CurrentUser

log = logging.getLogger(__name__)
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
    await state.agent_runtime.ensure(project)  # history/queue work even if a snapshot fails
    await touch_opened(state.db, project.id)
    version: str = await session.call(lambda h: str(h.kicad.get_version().full_version))
    # X1/X3: the viewer has content right after open, before any agent turn. A
    # snapshot that cannot be written leaves that viewer empty; it must not make
    # the (already open) project unusable for the agent.
    try:
        view_files = await write_view_snapshots(session)
    except KicadUnavailable:
        raise
    except (CopperChatError, OSError, ApiError) as exc:
        log.warning("view snapshots for %s failed: %s", project.rel_path, exc)
        view_files = {"sch": [], "pcb": []}
    return {
        "id": project.id,
        "readOnly": session.read_only,
        "lockFiles": sorted(p.name for p in session.lock_files),
        "kicadVersion": version,
        "viewFiles": view_files,
    }
