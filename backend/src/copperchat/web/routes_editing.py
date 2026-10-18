"""Editing API (E-C12): settings (E1), change log, diff and ERC results (E8a)."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict

from copperchat.changes.diff import cached_diff
from copperchat.changes.repo import ChangeEntry
from copperchat.db.repos.editing_settings import EditingSettings, EditingSettingsRepo
from copperchat.editing.service import EditingService
from copperchat.errors import CopperChatError
from copperchat.history.service import HistoryService
from copperchat.projects import Project, get_project
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


# -- change log, diff, ERC results (E8a) ------------------------------------------

_RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,100}")
RUNS_LIMIT_MAX = 100


class InvalidChangeQuery(CopperChatError):
    code = "invalid_argument"
    http_status = 400


def _services(request: Request, project_id: str) -> tuple[EditingService, HistoryService, Project]:
    project = get_project(request.app.state.settings.projects_root, project_id)
    return request.app.state.editing, request.app.state.history, project


def _run_or_step_id(value: str | None, name: str) -> str | None:
    if value is not None and _RUN_ID_RE.fullmatch(value) is None:
        raise InvalidChangeQuery(f"invalid {name}: {value!r}")
    return value


def _change_dto(e: ChangeEntry) -> dict[str, Any]:
    return {
        "id": e.id,
        "runId": e.run_id,
        "planStepId": e.plan_step_id,
        "callSeq": e.call_seq,
        "tool": e.tool,
        "op": e.op,
        "kind": e.kind,
        "uuid": e.uuid,
        "sheetPath": e.sheet_path,
        "ref": e.ref,
        "net": e.net,
        "before": e.before,
        "after": e.after,
        "sideEffect": e.side_effect,
        "createdAt": e.created_at,
    }


def _diff_entry_dto(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": entry["id"],
        "kind": entry["kind"],
        "type": entry["type"],
        "uuid": entry["uuid"],
        "ref": entry["ref"],
        "boxMm": entry["box_mm"],
        "properties": entry["properties"],
        "sideEffect": entry["side_effect"],
    }


@router.get("/projects/{project_id}/changes/runs")
async def list_change_runs(
    project_id: str,
    request: Request,
    _: CurrentUser,
    limit: int = Query(20, ge=1, le=RUNS_LIMIT_MAX),
) -> list[dict[str, Any]]:
    editing, history, project = _services(request, project_id)
    runs = await editing.changes.runs(project.id, limit)
    ercs = await editing.erc_results.latest_for_runs(project.id, [r["run_id"] for r in runs])
    snaps = await history.store(project).latest_for_runs(r["run_id"] for r in runs)
    out: list[dict[str, Any]] = []
    for r in runs:
        erc = ercs.get(r["run_id"])
        snap = snaps.get(r["run_id"])
        out.append(
            {
                "runId": r["run_id"],
                "stepIds": r["step_ids"],
                "count": r["count"],
                "sideEffects": r["side_effects"],
                "erc": None
                if erc is None
                else {
                    "errors": len(erc["new_errors"]),
                    "warnings": len(erc["new_warnings"]),
                    "resultId": erc["id"],
                },
                "snapshotId": snap.id if snap is not None else None,
                "createdAt": r["created_at"],
            }
        )
    return out


@router.get("/projects/{project_id}/changes")
async def list_changes(
    project_id: str,
    request: Request,
    _: CurrentUser,
    runId: str = Query(...),
    stepId: str | None = Query(None),
) -> list[dict[str, Any]]:
    editing, _history, project = _services(request, project_id)
    run_id = _run_or_step_id(runId, "runId")
    assert run_id is not None
    step_id = _run_or_step_id(stepId, "stepId")
    return [_change_dto(e) for e in await editing.changes.for_run(project.id, run_id, step_id)]


@router.get("/projects/{project_id}/changes/diff")
async def changes_diff(
    project_id: str,
    request: Request,
    _: CurrentUser,
    snapshotId: str = Query(...),
) -> dict[str, Any]:
    editing, history, project = _services(request, project_id)
    # 400 invalid id / 404 unknown snapshot / 503 project not open in KiCad.
    snap = await history.store(project).get(snapshotId)
    stats = await editing.changes.since_stats(project.id, snap.created_at)
    diff = await cached_diff(
        history, project, snap, stats, lambda: editing.changes.since(project.id, snap.created_at)
    )
    return {
        "snapshotId": snap.id,
        "count": diff["count"],
        "sheets": {
            path: [_diff_entry_dto(e) for e in entries] for path, entries in diff["sheets"].items()
        },
    }


@router.get("/projects/{project_id}/erc-results/{result_id}")
async def get_erc_result(
    project_id: str, result_id: str, request: Request, _: CurrentUser
) -> dict[str, Any]:
    editing, _history, project = _services(request, project_id)
    row = await editing.erc_results.get(project.id, result_id)  # 400 bad id / 404 unknown
    return {
        "id": row["id"],
        "runId": row["run_id"],
        "planStepId": row["plan_step_id"],
        "baselineSnapshotId": row["baseline_snapshot_id"],
        "newErrors": row["new_errors"],
        "newWarnings": row["new_warnings"],
        "resolved": row["resolved"],
        "justifications": row["justifications"],
        "createdAt": row["created_at"],
    }
