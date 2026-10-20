"""Design docs REST (spec §6.1 "Docs route rules", plan S-C4/S-C6).

- The suffix routes are registered before the bare ``/docs/{path:path}`` routes: the
  ``path`` converter is greedy, so ``/docs/x.md/revisions`` would otherwise be served as
  the doc ``x.md/revisions``. The bare routes go through the path guard, which only
  accepts ``.md`` paths.
- Request bodies are read with a 1 MiB cap while streaming (also without
  ``Content-Length``); the 256 KiB file cap is checked on bytes by the service.
- Errors carry the doc error code and never a file-system path.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from copperchat.design_docs.paths import DocError
from copperchat.design_docs.service import DesignDocs
from copperchat.projects import Project, get_project
from copperchat.web.auth import CurrentUser

router = APIRouter(prefix="/api/projects/{project_id}/docs", tags=["docs"])

MAX_REQUEST_BYTES = 1024 * 1024


class _Body(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class PutBody(_Body):
    body: str
    base_version: str | None = Field(default=None, max_length=64)


class VersionBody(_Body):
    version: str = Field(min_length=1, max_length=64)


class RevertBody(_Body):
    base_version: str | None = Field(default=None, max_length=64)


def _project(request: Request, project_id: str) -> Project:
    return get_project(request.app.state.settings.projects_root, project_id)


def _docs(request: Request) -> DesignDocs:
    docs: DesignDocs = request.app.state.docs
    return docs


async def read_json[M: BaseModel](
    request: Request, model: type[M], limit: int = MAX_REQUEST_BYTES
) -> M:
    """Read and validate a JSON body, refusing more than ``limit`` bytes while streaming."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, "request body too large")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise HTTPException(413, "request body too large")
    try:
        return model.model_validate(json.loads(buf))
    except (ValueError, ValidationError) as exc:
        raise HTTPException(400, "invalid request body") from exc


def _error(err: DocError) -> JSONResponse:
    details = {to_camel(k): v for k, v in (err.details or {}).items()}
    return JSONResponse(
        status_code=err.http_status,
        content={
            "error": {
                "code": err.code,
                "message": f"{err.what}: {err.why}",
                "details": details,
            }
        },
    )


async def _run[T](awaitable: Awaitable[T]) -> T | JSONResponse:
    try:
        return await awaitable
    except DocError as err:
        return _error(err)


# ---- suffix routes first (route order, spec §6.1)


@router.post("/{path:path}/approve", response_model=None)
async def approve(
    request: Request, project_id: str, path: str, _: CurrentUser
) -> dict[str, Any] | JSONResponse:
    project = _project(request, project_id)
    body = await read_json(request, VersionBody)
    out = await _run(_docs(request).approve(project, path, body.version))
    return out if isinstance(out, JSONResponse) else {"version": out}


@router.post("/{path:path}/unapprove", response_model=None)
async def unapprove(
    request: Request, project_id: str, path: str, _: CurrentUser
) -> dict[str, Any] | JSONResponse:
    project = _project(request, project_id)
    body = await read_json(request, VersionBody)
    out = await _run(_docs(request).unapprove(project, path, body.version))
    return out if isinstance(out, JSONResponse) else {"version": out}


@router.post("/{path:path}/clear-user-edited", response_model=None)
async def clear_user_edited(
    request: Request, project_id: str, path: str, _: CurrentUser
) -> dict[str, Any] | JSONResponse:
    project = _project(request, project_id)
    body = await read_json(request, VersionBody)
    out = await _run(_docs(request).clear_user_edited(project, path, body.version))
    return out if isinstance(out, JSONResponse) else {"version": out}


@router.get("/{path:path}/revisions", response_model=None)
async def revisions(
    request: Request,
    project_id: str,
    path: str,
    _: CurrentUser,
    limit: int = Query(50, ge=1, le=100),
) -> list[dict[str, Any]] | JSONResponse:
    project = _project(request, project_id)
    out = await _run(_docs(request).revisions(project, path, limit))
    return out if isinstance(out, JSONResponse) else [r.dto() for r in out]


@router.get("/{path:path}/revisions/{rid:int}", response_model=None)
async def revision(
    request: Request, project_id: str, path: str, rid: int, _: CurrentUser
) -> dict[str, Any] | JSONResponse:
    project = _project(request, project_id)
    out = await _run(_docs(request).revision(project, path, rid))
    if isinstance(out, JSONResponse):
        return out
    return {"content": out.content, "prevContent": out.prev_content}


@router.post("/{path:path}/revisions/{rid:int}/revert", response_model=None)
async def revert(
    request: Request, project_id: str, path: str, rid: int, _: CurrentUser
) -> dict[str, Any] | JSONResponse:
    project = _project(request, project_id)
    body = await read_json(request, RevertBody)
    out = await _run(_docs(request).revert(project, path, rid, body.base_version))
    return out if isinstance(out, JSONResponse) else {"version": out}


# ---- index and bare doc routes


@router.get("", response_model=None)
async def index(request: Request, project_id: str, _: CurrentUser) -> dict[str, Any]:
    return (await _docs(request).list(_project(request, project_id))).dto()


@router.get("/{path:path}", response_model=None)
async def read_doc(
    request: Request, project_id: str, path: str, _: CurrentUser
) -> dict[str, Any] | JSONResponse:
    project = _project(request, project_id)
    out = await _run(_docs(request).read(project, path))
    if isinstance(out, JSONResponse):
        return out
    data: dict[str, Any] = {
        "path": out.path,
        "exists": out.exists,
        "version": out.version,
        "meta": out.meta(),
        "body": out.body,
    }
    if out.template is not None:
        data["template"] = out.template
    return data


@router.put("/{path:path}", response_model=None)
async def put_doc(
    request: Request, project_id: str, path: str, _: CurrentUser
) -> dict[str, Any] | JSONResponse:
    project = _project(request, project_id)
    body = await read_json(request, PutBody)
    out = await _run(
        _docs(request).write(project, path, body.body, body.base_version, author="user")
    )
    return out if isinstance(out, JSONResponse) else {"version": out.version}
