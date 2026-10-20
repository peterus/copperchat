"""Skills page API: three-layer listing, customise / edit / reset, per-project toggles."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from copperchat.db.repos.skill_prefs import disabled_skills, set_skill_enabled
from copperchat.projects import get_project
from copperchat.skills import store
from copperchat.skills.store import EffectiveSkill, SkillLayers
from copperchat.web.auth import CurrentUser

router = APIRouter(prefix="/api", tags=["skills"])


class ContentBody(BaseModel):
    content: str


class EnabledBody(BaseModel):
    enabled: bool


def _layers(request: Request, project_id: str | None) -> SkillLayers:
    settings = request.app.state.settings
    project = get_project(settings.projects_root, project_id) if project_id else None
    return SkillLayers.for_project(settings, project)


def _dto(s: EffectiveSkill, disabled: set[str]) -> dict[str, Any]:
    return {
        "name": s.name,
        "origin": s.origin,
        "description": s.description,
        "shadowed": list(s.shadowed),
        "hasBuiltin": s.has_builtin,
        "originUpdated": s.origin_updated,
        "enabled": s.name not in disabled,
        "agent": s.agent,
        # the session types the skill applies to (None = all), for the Skills GUI chips
        "sessions": None if s.sessions is None else sorted(s.sessions),
    }


def _call[T](fn: Callable[..., T], *args: Any) -> T:
    try:
        return fn(*args)
    except (store.InvalidSkillName, store.InvalidSkill) as exc:
        raise HTTPException(400, exc.message) from exc
    except store.SkillNotFound as exc:
        raise HTTPException(404, exc.message) from exc
    except store.SkillConflict as exc:
        raise HTTPException(409, exc.message) from exc


async def _disabled(request: Request, project_id: str | None) -> set[str]:
    return await disabled_skills(request.app.state.db, project_id) if project_id else set()


@router.get("/skills")
async def list_skills(
    request: Request,
    _: CurrentUser,
    projectId: str | None = None,
) -> list[dict[str, Any]]:
    layers = _layers(request, projectId)
    disabled = await _disabled(request, projectId)
    return [_dto(s, disabled) for s in store.effective_skills(layers)]


def _detail(layers: SkillLayers, skill: EffectiveSkill, disabled: set[str]) -> dict[str, Any]:
    md = skill.dir / "SKILL.md"
    return {
        **_dto(skill, disabled),
        "content": md.read_text(encoding="utf-8", errors="replace"),
        "originDiff": _call(store.origin_diff, layers, skill.name)
        if skill.origin == "user"
        else None,
        "editable": skill.origin == "user",
    }


@router.get("/skills/{name}")
async def skill_detail(
    request: Request,
    name: str,
    _: CurrentUser,
    projectId: str | None = None,
) -> dict[str, Any]:
    layers = _layers(request, projectId)
    skill = _call(store.get_skill, layers, name)
    return _detail(layers, skill, await _disabled(request, projectId))


@router.post("/skills/{name}/customise", status_code=201)
async def customise(request: Request, name: str, _: CurrentUser) -> dict[str, Any]:
    layers = _layers(request, None)
    _call(store.customise, layers, name)
    return _detail(layers, _call(store.get_skill, layers, name), set())


@router.put("/skills/user/{name}", status_code=204)
async def put_user_skill(
    request: Request, name: str, body: ContentBody, _: CurrentUser
) -> Response:
    _call(store.write_user_skill, _layers(request, None), name, body.content)
    return Response(status_code=204)


@router.post("/skills/{name}/reset", status_code=204)
async def reset(request: Request, name: str, _: CurrentUser) -> Response:
    _call(store.reset, _layers(request, None), name)
    return Response(status_code=204)


@router.post("/skills/{name}/acknowledge-update", status_code=204)
async def acknowledge(request: Request, name: str, _: CurrentUser) -> Response:
    _call(store.acknowledge_update, _layers(request, None), name)
    return Response(status_code=204)


@router.put("/projects/{project_id}/skills/{name}", status_code=204)
async def set_enabled(
    request: Request, project_id: str, name: str, body: EnabledBody, _: CurrentUser
) -> Response:
    _call(store.validate_skill_name, name)
    project = get_project(request.app.state.settings.projects_root, project_id)
    await set_skill_enabled(request.app.state.db, project.id, name, body.enabled)
    return Response(status_code=204)
