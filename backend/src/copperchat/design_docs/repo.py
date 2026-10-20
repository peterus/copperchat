"""``doc_revisions`` and ``doc_approvals`` (migration 0009, spec §7.3 / §7.6)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from copperchat.db import Database, utcnow_iso

MAX_REVISIONS_PER_DOC = 100
MAX_REVISION_BYTES_PER_PROJECT = 32 * 1024 * 1024

# Every column except `content` (lists and the index never load revision text).
_META = (
    "id, path, version, prev_version, added, removed, bytes, author, conversation_id, "
    "run_id, summary, conversation_label, created_at"
)


@dataclass(frozen=True)
class RevisionRow:
    id: int
    path: str
    version: str
    prev_version: str | None
    added: int
    removed: int
    bytes: int
    author: str
    conversation_id: str | None
    run_id: str | None
    summary: str
    conversation_label: str | None
    created_at: str

    @classmethod
    def of(cls, row: Any) -> RevisionRow:
        return cls(**{k: row[k] for k in cls.__dataclass_fields__})

    def dto(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "prevVersion": self.prev_version,
            "author": self.author,
            "conversationId": self.conversation_id,
            "conversationTitle": self.conversation_label,
            "runId": self.run_id,
            "summary": self.summary,
            "createdAt": self.created_at,
            "added": self.added,
            "removed": self.removed,
        }


@dataclass(frozen=True)
class Approval:
    body_version: str
    approved_at: str


class DocRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def insert_revision(
        self,
        project_id: str,
        path: str,
        *,
        version: str,
        prev_version: str | None,
        added: int,
        removed: int,
        author: str,
        content: str,
        summary: str = "",
        conversation_id: str | None = None,
        run_id: str | None = None,
        conversation_label: str | None = None,
    ) -> int:
        return await self.db.execute(
            "INSERT INTO doc_revisions (project_id, path, version, prev_version, added, "
            "removed, bytes, author, conversation_id, run_id, summary, conversation_label, "
            "content, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                project_id,
                path,
                version,
                prev_version,
                added,
                removed,
                len(content.encode("utf-8")),
                author,
                conversation_id,
                run_id,
                summary,
                conversation_label,
                content,
                utcnow_iso(),
            ),
        )

    async def latest(self, project_id: str, path: str) -> RevisionRow | None:
        row = await self.db.fetchone(
            f"SELECT {_META} FROM doc_revisions WHERE project_id = ? AND path = ? "
            "ORDER BY id DESC LIMIT 1",
            (project_id, path),
        )
        return None if row is None else RevisionRow.of(row)

    async def latest_per_path(self, project_id: str) -> dict[str, RevisionRow]:
        """Newest revision (without content) of every doc: one grouped query."""
        rows = await self.db.fetchall(
            f"SELECT {', '.join('r.' + c.strip() for c in _META.split(','))} "
            "FROM doc_revisions r JOIN (SELECT MAX(id) AS id FROM doc_revisions "
            "WHERE project_id = ? GROUP BY path) m ON r.id = m.id",
            (project_id,),
        )
        return {r["path"]: RevisionRow.of(r) for r in rows}

    async def content_of(self, project_id: str, path: str, rid: int) -> str | None:
        row = await self.db.fetchone(
            "SELECT content FROM doc_revisions WHERE project_id = ? AND path = ? AND id = ?",
            (project_id, path, rid),
        )
        return None if row is None else str(row["content"])

    async def content_by_version(self, project_id: str, path: str, version: str) -> str | None:
        row = await self.db.fetchone(
            "SELECT content FROM doc_revisions WHERE project_id = ? AND path = ? "
            "AND version = ? ORDER BY id DESC LIMIT 1",
            (project_id, path, version),
        )
        return None if row is None else str(row["content"])

    async def list(self, project_id: str, path: str, limit: int) -> list[RevisionRow]:
        rows = await self.db.fetchall(
            f"SELECT {_META} FROM doc_revisions WHERE project_id = ? AND path = ? "
            "ORDER BY id DESC LIMIT ?",
            (project_id, path, limit),
        )
        return [RevisionRow.of(r) for r in rows]

    async def get_with_prev(
        self, project_id: str, path: str, rid: int
    ) -> tuple[str, str | None] | None:
        """(content, previous revision's content) - scoped by project and path."""
        content = await self.content_of(project_id, path, rid)
        if content is None:
            return None
        prev = await self.db.fetchone(
            "SELECT content FROM doc_revisions WHERE project_id = ? AND path = ? AND id < ? "
            "ORDER BY id DESC LIMIT 1",
            (project_id, path, rid),
        )
        return content, None if prev is None else str(prev["content"])

    # ---- approvals

    async def approvals(self, project_id: str) -> dict[str, Approval]:
        rows = await self.db.fetchall(
            "SELECT path, body_version, approved_at FROM doc_approvals WHERE project_id = ?",
            (project_id,),
        )
        return {r["path"]: Approval(r["body_version"], r["approved_at"]) for r in rows}

    async def approval(self, project_id: str, path: str) -> Approval | None:
        row = await self.db.fetchone(
            "SELECT body_version, approved_at FROM doc_approvals WHERE project_id = ? AND path = ?",
            (project_id, path),
        )
        return None if row is None else Approval(row["body_version"], row["approved_at"])

    async def set_approval(self, project_id: str, path: str, version: str) -> Approval:
        now = utcnow_iso()
        await self.db.execute(
            "INSERT INTO doc_approvals (project_id, path, body_version, approved_at) "
            "VALUES (?,?,?,?) ON CONFLICT (project_id, path) DO UPDATE SET "
            "body_version = excluded.body_version, approved_at = excluded.approved_at",
            (project_id, path, version, now),
        )
        return Approval(version, now)

    async def delete_approval(self, project_id: str, path: str) -> None:
        await self.db.execute(
            "DELETE FROM doc_approvals WHERE project_id = ? AND path = ?", (project_id, path)
        )

    # ---- retention

    async def prune(self, project_id: str) -> int:
        """Keep the newest 100 revisions per doc and at most 32 MiB of revision content
        per project (oldest pruned first). The newest revision of each doc and every
        revision of an approved body version are never pruned."""
        rows = await self.db.fetchall(
            "SELECT id, path, version, bytes FROM doc_revisions WHERE project_id = ? "
            "ORDER BY id DESC",
            (project_id,),
        )
        approved = {(p, a.body_version) for p, a in (await self.approvals(project_id)).items()}
        per_doc: dict[str, int] = {}
        total = 0
        doomed: list[int] = []
        for r in rows:
            n = per_doc.get(r["path"], 0)
            per_doc[r["path"]] = n + 1
            keep_always = n == 0 or (r["path"], r["version"]) in approved
            if not keep_always and (
                n >= MAX_REVISIONS_PER_DOC or total + r["bytes"] > MAX_REVISION_BYTES_PER_PROJECT
            ):
                doomed.append(r["id"])
                continue
            total += r["bytes"]
        for i in range(0, len(doomed), 500):
            chunk = doomed[i : i + 500]
            await self.db.execute(
                f"DELETE FROM doc_revisions WHERE id IN ({','.join('?' * len(chunk))})",
                chunk,
            )
        return len(doomed)
