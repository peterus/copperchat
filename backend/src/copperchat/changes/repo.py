"""Persistence of the change journal (`changes` table, E-C3)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import aiosqlite

from copperchat.db import Database, utcnow_iso

_COLUMNS = (
    "project_id, run_id, plan_step_id, call_seq, tool, op, kind, uuid, sheet_path, "
    "ref, net, before, after, side_effect, created_at"
)


@dataclass
class ChangeEntry:
    project_id: str
    run_id: str
    plan_step_id: str | None
    call_seq: int
    tool: str
    op: str  # add | modify | delete
    kind: str
    uuid: str
    sheet_path: str
    ref: str | None = None
    net: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    side_effect: bool = False
    id: int | None = None
    created_at: str | None = None


def _dump(value: dict[str, Any] | None) -> str | None:
    return None if value is None else json.dumps(value, separators=(",", ":"))


def _load(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    loaded: dict[str, Any] = json.loads(value)
    return loaded


def _entry(row: aiosqlite.Row) -> ChangeEntry:
    return ChangeEntry(
        id=int(row["id"]),
        project_id=str(row["project_id"]),
        run_id=str(row["run_id"]),
        plan_step_id=row["plan_step_id"],
        call_seq=int(row["call_seq"]),
        tool=str(row["tool"]),
        op=str(row["op"]),
        kind=str(row["kind"]),
        uuid=str(row["uuid"]),
        sheet_path=str(row["sheet_path"]),
        ref=row["ref"],
        net=row["net"],
        before=_load(row["before"]),
        after=_load(row["after"]),
        side_effect=bool(row["side_effect"]),
        created_at=str(row["created_at"]),
    )


class ChangeRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def add_many(self, entries: Iterable[ChangeEntry]) -> int:
        """Insert all entries in one transaction; returns how many were written."""
        items = list(entries)
        if not items:
            return 0
        now = utcnow_iso()
        async with self._db.transaction():
            for e in items:
                await self._db.execute(
                    f"INSERT INTO changes({_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        e.project_id,
                        e.run_id,
                        e.plan_step_id,
                        e.call_seq,
                        e.tool,
                        e.op,
                        e.kind,
                        e.uuid,
                        e.sheet_path,
                        e.ref,
                        e.net,
                        _dump(e.before),
                        _dump(e.after),
                        1 if e.side_effect else 0,
                        e.created_at or now,
                    ),
                )
        return len(items)

    async def for_run(
        self, project_id: str, run_id: str, step_id: str | None = None
    ) -> list[ChangeEntry]:
        sql = f"SELECT id, {_COLUMNS} FROM changes WHERE project_id = ? AND run_id = ?"
        params: list[Any] = [project_id, run_id]
        if step_id is not None:
            sql += " AND plan_step_id = ?"
            params.append(step_id)
        rows = await self._db.fetchall(sql + " ORDER BY id", params)
        return [_entry(r) for r in rows]

    async def since(self, project_id: str, created_at: str) -> list[ChangeEntry]:
        """Every row written at or after `created_at` (e.g. since a snapshot)."""
        rows = await self._db.fetchall(
            f"SELECT id, {_COLUMNS} FROM changes WHERE project_id = ? AND created_at >= ?"
            " ORDER BY id",
            (project_id, created_at),
        )
        return [_entry(r) for r in rows]

    async def since_stats(self, project_id: str, created_at: str) -> tuple[int, int]:
        """`(max id, count)` of the rows `since` would return, without loading them."""
        row = await self._db.fetchone(
            "SELECT MAX(id) AS last_id, COUNT(*) AS n FROM changes"
            " WHERE project_id = ? AND created_at >= ?",
            (project_id, created_at),
        )
        return (int(row["last_id"] or 0), int(row["n"])) if row else (0, 0)

    async def for_steps(self, project_id: str, step_ids: list[str]) -> list[ChangeEntry]:
        """Rows of any of the plan steps (one plan), in write order."""
        if not step_ids:
            return []
        marks = ", ".join("?" for _ in step_ids)
        rows = await self._db.fetchall(
            f"SELECT id, {_COLUMNS} FROM changes WHERE project_id = ?"
            f" AND plan_step_id IN ({marks}) ORDER BY id",
            (project_id, *step_ids),
        )
        return [_entry(r) for r in rows]

    async def runs(self, project_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Per run, newest first: step ids (first-seen order), counts and first timestamp."""
        summaries = await self._db.fetchall(
            "SELECT s.run_id, s.count, s.side_effects, f.created_at FROM ("
            "SELECT run_id, COUNT(*) AS count, SUM(side_effect) AS side_effects, "
            "MIN(id) AS first_id, MAX(id) AS last_id FROM changes WHERE project_id = ? "
            "GROUP BY run_id ORDER BY last_id DESC LIMIT ?) AS s "
            "JOIN changes AS f ON f.id = s.first_id ORDER BY s.last_id DESC",
            (project_id, limit),
        )
        if not summaries:
            return []
        run_ids = [str(s["run_id"]) for s in summaries]
        marks = ", ".join("?" for _ in run_ids)
        step_rows = await self._db.fetchall(
            "SELECT run_id, plan_step_id, MIN(id) AS first_id FROM changes "
            f"WHERE project_id = ? AND run_id IN ({marks}) AND plan_step_id IS NOT NULL "
            "GROUP BY run_id, plan_step_id ORDER BY first_id",
            (project_id, *run_ids),
        )
        steps: dict[str, list[str]] = {}
        for r in step_rows:
            steps.setdefault(str(r["run_id"]), []).append(str(r["plan_step_id"]))
        return [
            {
                "run_id": str(s["run_id"]),
                "step_ids": steps.get(str(s["run_id"]), []),
                "count": int(s["count"]),
                "side_effects": int(s["side_effects"] or 0),
                "created_at": str(s["created_at"]) if s["created_at"] else None,
            }
            for s in summaries
        ]
