from __future__ import annotations

from collections.abc import Iterable

from copperchat.db import Database, utcnow_iso
from copperchat.projects import Project


async def upsert_projects(db: Database, projects: Iterable[Project]) -> None:
    async with db.transaction():
        for p in projects:
            await db.execute(
                "INSERT INTO projects(id, rel_path, name) VALUES (?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET rel_path=excluded.rel_path, name=excluded.name",
                (p.id, p.rel_path, p.name),
            )


async def touch_opened(db: Database, project_id: str) -> None:
    await db.execute(
        "UPDATE projects SET last_opened_at = ? WHERE id = ?", (utcnow_iso(), project_id)
    )
