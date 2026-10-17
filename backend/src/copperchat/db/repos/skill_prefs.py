"""Per-project skill toggles (skills are enabled unless a row says otherwise)."""

from __future__ import annotations

from copperchat.db import Database


async def set_skill_enabled(db: Database, project_id: str, skill_name: str, enabled: bool) -> None:
    await db.execute(
        "INSERT INTO skill_prefs(project_id, skill_name, enabled) VALUES (?, ?, ?) "
        "ON CONFLICT(project_id, skill_name) DO UPDATE SET enabled = excluded.enabled",
        (project_id, skill_name, 1 if enabled else 0),
    )


async def disabled_skills(db: Database, project_id: str) -> set[str]:
    rows = await db.fetchall(
        "SELECT skill_name FROM skill_prefs WHERE project_id = ? AND enabled = 0", (project_id,)
    )
    return {str(r["skill_name"]) for r in rows}
