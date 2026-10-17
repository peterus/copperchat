"""Chat attachments uploaded through POST /api/projects/{id}/uploads."""

from __future__ import annotations

from dataclasses import astuple, dataclass

from copperchat.db import Database


@dataclass(frozen=True)
class UploadRow:
    id: str
    project_id: str
    kind: str
    name: str
    rel_path: str
    mime: str
    size: int
    created_at: str


async def insert_upload(db: Database, row: UploadRow) -> None:
    await db.execute(
        "INSERT INTO uploads(id, project_id, kind, name, rel_path, mime, size, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        astuple(row),
    )


async def get_upload(db: Database, project_id: str, upload_id: str) -> UploadRow | None:
    r = await db.fetchone(
        "SELECT id, project_id, kind, name, rel_path, mime, size, created_at "
        "FROM uploads WHERE project_id = ? AND id = ?",
        (project_id, upload_id),
    )
    return None if r is None else UploadRow(*tuple(r))
