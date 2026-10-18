"""`plans` / `plan_steps` persistence (E-C3)."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from copperchat.db import Database
from copperchat.errors import CopperChatError
from copperchat.plans.model import OPEN, Plan, PlanStateError, PlanStep

_ID_RE = re.compile(r"[0-9a-f]{32}")
# Inlined literals (not bound parameters) so SQLite can use the ux_plans_open partial
# index, whose predicate is exactly this list.
_OPEN_SQL = "('proposed', 'approved', 'running', 'paused')"
assert set(_OPEN_SQL.strip("()").replace("'", "").split(", ")) == OPEN


class InvalidPlanId(CopperChatError):
    code = "invalid_argument"
    http_status = 400


class PlanNotFound(CopperChatError):
    code = "not_found"
    http_status = 404


def validate_plan_id(raw: str) -> str:
    if not isinstance(raw, str) or _ID_RE.fullmatch(raw) is None:
        raise InvalidPlanId(f"invalid plan id: {raw!r}")
    return raw


def _step(row: Any) -> PlanStep:
    return PlanStep(
        id=str(row["id"]),
        plan_id=str(row["plan_id"]),
        idx=int(row["idx"]),
        title=str(row["title"]),
        intent=str(row["intent"]),
        expected_changes=int(row["expected_changes"]),
        touches=json.loads(row["touches"]),
        verification=str(row["verification"]),
        status=row["status"],
        run_id=row["run_id"],
        checkpoint_id=row["checkpoint_id"],
        change_count=int(row["change_count"]),
        summary=row["summary"],
        failure=row["failure"],
        erc_result_id=row["erc_result_id"],
    )


class PlanRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def _load(self, row: Any) -> Plan:
        steps = await self._db.fetchall(
            "SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY idx", (row["id"],)
        )
        return Plan(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            title=str(row["title"]),
            summary=str(row["summary"]),
            status=row["status"],
            revision=int(row["revision"]),
            created_run_id=str(row["created_run_id"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            steps=tuple(_step(s) for s in steps),
            event_seq=int(row["event_seq"]),
            pause_reason=row["pause_reason"],
            base_snapshot_id=row["base_snapshot_id"],
        )

    async def get(self, project_id: str, plan_id: str) -> Plan:
        pid = validate_plan_id(plan_id)
        row = await self._db.fetchone(
            "SELECT * FROM plans WHERE id = ? AND project_id = ?", (pid, project_id)
        )
        if row is None:
            raise PlanNotFound(f"plan {pid} not found")
        return await self._load(row)

    async def open_for(self, project_id: str) -> Plan | None:
        row = await self._db.fetchone(
            f"SELECT * FROM plans WHERE project_id = ? AND status IN {_OPEN_SQL}",
            (project_id,),
        )
        return None if row is None else await self._load(row)

    async def open_status(self, project_id: str) -> tuple[str, str] | None:
        """(plan id, status) of the open plan without loading its steps (gate fast path)."""
        row = await self._db.fetchone(
            f"SELECT id, status FROM plans WHERE project_id = ? AND status IN {_OPEN_SQL}",
            (project_id,),
        )
        return None if row is None else (str(row["id"]), str(row["status"]))

    async def running(self) -> list[Plan]:
        rows = await self._db.fetchall("SELECT * FROM plans WHERE status = 'running'")
        return [await self._load(r) for r in rows]

    async def protected_snapshot_ids(self, project_id: str) -> set[str]:
        """Snapshots an open plan may still roll back to (base + step checkpoints)."""
        rows = await self._db.fetchall(
            "SELECT p.base_snapshot_id AS sid FROM plans p"
            f" WHERE p.project_id = ? AND p.status IN {_OPEN_SQL}"
            " UNION SELECT s.checkpoint_id FROM plan_steps s JOIN plans p ON p.id = s.plan_id"
            f" WHERE p.project_id = ? AND p.status IN {_OPEN_SQL}",
            (project_id, project_id),
        )
        return {str(r["sid"]) for r in rows if r["sid"] is not None}

    async def _write_steps(self, plan: Plan) -> None:
        await self._db.execute("DELETE FROM plan_steps WHERE plan_id = ?", (plan.id,))
        for s in plan.steps:
            await self._db.execute(
                "INSERT INTO plan_steps(id, plan_id, idx, title, intent, expected_changes,"
                " touches, verification, status, run_id, checkpoint_id, change_count, summary,"
                " failure, erc_result_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    s.id,
                    plan.id,
                    s.idx,
                    s.title,
                    s.intent,
                    s.expected_changes,
                    json.dumps(s.touches),
                    s.verification,
                    s.status,
                    s.run_id,
                    s.checkpoint_id,
                    s.change_count,
                    s.summary,
                    s.failure,
                    s.erc_result_id,
                ),
            )

    async def insert(self, plan: Plan) -> Plan:
        """A second open plan of the project violates `ux_plans_open` -> `plan_open`."""
        try:
            async with self._db.transaction():
                await self._db.execute(
                    "INSERT INTO plans(id, project_id, title, summary, status, revision,"
                    " event_seq, pause_reason, base_snapshot_id, created_run_id, created_at,"
                    " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        plan.id,
                        plan.project_id,
                        plan.title,
                        plan.summary,
                        plan.status,
                        plan.revision,
                        plan.event_seq,
                        plan.pause_reason,
                        plan.base_snapshot_id,
                        plan.created_run_id,
                        plan.created_at,
                        plan.updated_at,
                    ),
                )
                await self._write_steps(plan)
        except sqlite3.IntegrityError as exc:
            raise PlanStateError(
                "A plan is already open for this project",
                "At most one plan can be proposed, approved, running or paused at a time",
                "Revise the open plan with plan_revise, or ask the user to finish, reject "
                "or abort it first.",
                code="plan_open",
            ) from exc
        return plan

    async def save(self, plan: Plan) -> Plan:
        """Write the plan's current state (row + steps) in one transaction."""
        async with self._db.transaction():
            await self._db.execute(
                "UPDATE plans SET title = ?, summary = ?, status = ?, revision = ?,"
                " event_seq = ?, pause_reason = ?, base_snapshot_id = ?, updated_at = ?"
                " WHERE id = ? AND project_id = ?",
                (
                    plan.title,
                    plan.summary,
                    plan.status,
                    plan.revision,
                    plan.event_seq,
                    plan.pause_reason,
                    plan.base_snapshot_id,
                    plan.updated_at,
                    plan.id,
                    plan.project_id,
                ),
            )
            await self._write_steps(plan)
        return plan

    async def step_change_count(self, project_id: str, run_id: str, step_id: str) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM changes WHERE project_id = ? AND run_id = ?"
            " AND plan_step_id = ?",
            (project_id, run_id, step_id),
        )
        return 0 if row is None else int(row["n"])

    async def erc_rows(self, plan: Plan) -> dict[str, dict[str, Any]]:
        """step id -> its `erc_results` summary (error/warning counts, justifications);
        the violation lists are counted in SQL, never decoded."""
        ids = [s.erc_result_id for s in plan.steps if s.erc_result_id]
        if not ids:
            return {}
        marks = ", ".join("?" for _ in ids)
        rows = await self._db.fetchall(
            "SELECT id, json_array_length(new_errors) AS errors,"
            " json_array_length(new_warnings) AS warnings, justifications"
            f" FROM erc_results WHERE project_id = ? AND id IN ({marks})",
            (plan.project_id, *ids),
        )
        by_id = {str(r["id"]): r for r in rows}
        out: dict[str, dict[str, Any]] = {}
        for s in plan.steps:
            r = by_id.get(s.erc_result_id or "")
            if r is not None:
                out[s.id] = {
                    "id": str(r["id"]),
                    "errors": int(r["errors"]),
                    "warnings": int(r["warnings"]),
                    "justifications": json.loads(r["justifications"]),
                }
        return out
