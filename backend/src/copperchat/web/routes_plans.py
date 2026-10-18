"""Plan REST (E-C12, spec §7.2): open plan, plan by id, user events and rollback."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from copperchat.plans.repo import validate_plan_id
from copperchat.plans.service import PlanService
from copperchat.projects import Project, get_project
from copperchat.web.auth import CurrentUser

router = APIRouter(prefix="/api/projects", tags=["plans"])


class RollbackBody(BaseModel):
    """`toStep` is validated by the service (400 outside 0..steps)."""

    model_config = ConfigDict(extra="forbid")

    toStep: Any = None


def _ctx(request: Request, project_id: str) -> tuple[PlanService, Project]:
    project = get_project(request.app.state.settings.projects_root, project_id)
    return request.app.state.plans, project


@router.get("/{project_id}/plans/open")
async def open_plan(project_id: str, request: Request, _: CurrentUser) -> dict[str, Any] | None:
    svc, project = _ctx(request, project_id)
    plan = await svc.open_plan(project)
    return None if plan is None else await svc.dto(plan)


@router.get("/{project_id}/plans/{plan_id}")
async def get_plan(
    project_id: str, plan_id: str, request: Request, _: CurrentUser
) -> dict[str, Any]:
    svc, project = _ctx(request, project_id)
    return await svc.dto(await svc.get(project, plan_id))


async def _event(request: Request, project_id: str, plan_id: str, event: str) -> dict[str, Any]:
    svc, project = _ctx(request, project_id)
    validate_plan_id(plan_id)
    # 400 invalid id, 404 unknown plan, 409 not allowed in the plan's state.
    plan = await getattr(svc, event)(project, plan_id)
    return await svc.dto(plan)


@router.post("/{project_id}/plans/{plan_id}/approve")
async def approve(
    project_id: str, plan_id: str, request: Request, _: CurrentUser
) -> dict[str, Any]:
    return await _event(request, project_id, plan_id, "approve")


@router.post("/{project_id}/plans/{plan_id}/reject")
async def reject(project_id: str, plan_id: str, request: Request, _: CurrentUser) -> dict[str, Any]:
    return await _event(request, project_id, plan_id, "reject")


@router.post("/{project_id}/plans/{plan_id}/resume")
async def resume(project_id: str, plan_id: str, request: Request, _: CurrentUser) -> dict[str, Any]:
    return await _event(request, project_id, plan_id, "resume")


@router.post("/{project_id}/plans/{plan_id}/abort")
async def abort(project_id: str, plan_id: str, request: Request, _: CurrentUser) -> dict[str, Any]:
    return await _event(request, project_id, plan_id, "abort")


@router.post("/{project_id}/plans/{plan_id}/rollback")
async def rollback(
    project_id: str, plan_id: str, body: RollbackBody, request: Request, _: CurrentUser
) -> dict[str, Any]:
    svc, project = _ctx(request, project_id)
    validate_plan_id(plan_id)
    # 409 agent_busy while a run is active (like history restore).
    plan = await svc.rollback(project, plan_id, body.toStep)
    return await svc.dto(plan)
