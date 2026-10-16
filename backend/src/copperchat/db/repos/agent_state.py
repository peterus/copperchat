"""Runs, SDK session ids and rate-limit state per project."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from copperchat.db import Database, utcnow_iso


class RunRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def start(self, run_id: str, project_id: str) -> None:
        await self._db.execute(
            "INSERT INTO runs (id, project_id, started_at) VALUES (?, ?, ?)",
            (run_id, project_id, utcnow_iso()),
        )

    async def finish(self, run_id: str, reason: str, usage: dict[str, Any] | None) -> None:
        await self._db.execute(
            "UPDATE runs SET finished_at = ?, reason = ?, usage = ? WHERE id = ?",
            (utcnow_iso(), reason, json.dumps(usage) if usage is not None else None, run_id),
        )

    async def get(self, run_id: str) -> dict[str, Any] | None:
        row = await self._db.fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))
        if row is None:
            return None
        out = dict(row)
        out["usage"] = json.loads(out["usage"]) if out["usage"] else None
        return out


class AgentSessionRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, project_id: str) -> str | None:
        row = await self._db.fetchone(
            "SELECT sdk_session_id FROM agent_sessions WHERE project_id = ?", (project_id,)
        )
        return None if row is None else str(row["sdk_session_id"])

    async def set(self, project_id: str, sdk_session_id: str) -> None:
        await self._db.execute(
            "INSERT INTO agent_sessions (project_id, sdk_session_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(project_id) DO UPDATE SET sdk_session_id = excluded.sdk_session_id, "
            "updated_at = excluded.updated_at",
            (project_id, sdk_session_id, utcnow_iso()),
        )


class RateLimitRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, project_id: str) -> datetime | None:
        row = await self._db.fetchone(
            "SELECT resets_at FROM rate_limit_state WHERE project_id = ?", (project_id,)
        )
        if row is None or row["resets_at"] is None:
            return None
        return datetime.fromisoformat(str(row["resets_at"]))

    async def set(self, project_id: str, resets_at: datetime | None) -> None:
        await self._db.execute(
            "INSERT INTO rate_limit_state (project_id, resets_at) VALUES (?, ?) "
            "ON CONFLICT(project_id) DO UPDATE SET resets_at = excluded.resets_at",
            (project_id, resets_at.isoformat() if resets_at is not None else None),
        )
