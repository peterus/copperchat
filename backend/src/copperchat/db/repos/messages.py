"""Chat message persistence."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

import aiosqlite

from copperchat.db import Database, utcnow_iso

utcnow = utcnow_iso  # contract: Phase 7 imports messages.utcnow

_COLS = (
    "id, project_id, conversation_id, client_message_id, run_id, role, kind, content, status, "
    "metadata, created_at, updated_at"
)


@dataclass(frozen=True)
class MessageRow:
    id: int
    project_id: str
    conversation_id: str
    client_message_id: str | None
    run_id: str | None
    role: str
    kind: str
    content: str
    status: str
    metadata: dict[str, Any]
    created_at: str
    updated_at: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "projectId": self.project_id,
            "conversationId": self.conversation_id,
            "clientMessageId": self.client_message_id,
            "runId": self.run_id,
            "role": self.role,
            "kind": self.kind,
            "content": self.content,
            "status": self.status,
            "metadata": self.metadata,
            "createdAt": self.created_at,
        }


def _row(r: aiosqlite.Row) -> MessageRow:
    return MessageRow(
        id=int(r["id"]),
        project_id=str(r["project_id"]),
        conversation_id=str(r["conversation_id"]),
        client_message_id=r["client_message_id"],
        run_id=r["run_id"],
        role=str(r["role"]),
        kind=str(r["kind"]),
        content=str(r["content"]),
        status=str(r["status"]),
        metadata=json.loads(r["metadata"] or "{}"),
        created_at=str(r["created_at"]),
        updated_at=str(r["updated_at"]),
    )


class MessageRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, message_id: int) -> MessageRow | None:
        r = await self._db.fetchone(f"SELECT {_COLS} FROM messages WHERE id = ?", (message_id,))
        return None if r is None else _row(r)

    async def get_by_client_id(self, project_id: str, client_message_id: str) -> MessageRow | None:
        r = await self._db.fetchone(
            f"SELECT {_COLS} FROM messages WHERE project_id = ? AND client_message_id = ?",
            (project_id, client_message_id),
        )
        return None if r is None else _row(r)

    async def insert(
        self,
        project_id: str,
        conversation_id: str,
        *,
        role: str,
        kind: str,
        content: str = "",
        status: str = "done",
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        client_message_id: str | None = None,
    ) -> int:
        now = utcnow_iso()
        async with self._db.transaction():
            mid = await self._db.execute(
                "INSERT INTO messages (project_id, conversation_id, client_message_id, run_id, "
                "role, kind, content, status, metadata, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    conversation_id,
                    client_message_id,
                    run_id,
                    role,
                    kind,
                    content,
                    status,
                    json.dumps(metadata or {}),
                    now,
                    now,
                ),
            )
            # ConversationRepo.touch, inside the same transaction.
            await self._db.execute(
                "UPDATE conversations SET last_message_at = ?, updated_at = ? WHERE id = ?",
                (now, now, conversation_id),
            )
        return mid

    async def insert_user(
        self,
        project_id: str,
        conversation_id: str,
        client_message_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[MessageRow, bool]:
        """Insert a queued user message. A retransmit of the same clientMessageId
        returns the original row and created=False (no second row, no re-queue)."""
        existing = await self.get_by_client_id(project_id, client_message_id)
        if existing is not None:
            return existing, False
        try:
            new_id = await self.insert(
                project_id,
                conversation_id,
                role="user",
                kind="text",
                content=text,
                status="queued",
                metadata=metadata,
                client_message_id=client_message_id,
            )
        except sqlite3.IntegrityError:
            raced = await self.get_by_client_id(project_id, client_message_id)
            if raced is None:
                raise
            return raced, False
        row = await self.get(new_id)
        if row is None:
            raise RuntimeError(f"message {new_id} vanished after insert")
        return row, True

    async def page(
        self, project_id: str, conversation_id: str, *, limit: int, before: int | None = None
    ) -> tuple[list[MessageRow], bool]:
        # The project filter stays as defence; the conversation index serves the scan.
        if before is None:
            rows = await self._db.fetchall(
                f"SELECT {_COLS} FROM messages WHERE conversation_id = ? AND project_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (conversation_id, project_id, limit + 1),
            )
        else:
            rows = await self._db.fetchall(
                f"SELECT {_COLS} FROM messages WHERE conversation_id = ? AND project_id = ? "
                "AND id < ? ORDER BY id DESC LIMIT ?",
                (conversation_id, project_id, before, limit + 1),
            )
        has_more = len(rows) > limit
        return [_row(r) for r in reversed(rows[:limit])], has_more

    async def since(
        self, project_id: str, conversation_id: str, since_id: int
    ) -> list[MessageRow]:
        rows = await self._db.fetchall(
            f"SELECT {_COLS} FROM messages WHERE conversation_id = ? AND project_id = ? "
            "AND id > ? ORDER BY id",
            (conversation_id, project_id, since_id),
        )
        return [_row(r) for r in rows]

    async def set_content(self, message_id: int, content: str) -> None:
        await self._db.execute(
            "UPDATE messages SET content = ?, updated_at = ? WHERE id = ?",
            (content, utcnow_iso(), message_id),
        )

    async def set_status(self, message_id: int, status: str) -> None:
        await self._db.execute(
            "UPDATE messages SET status = ?, updated_at = ? WHERE id = ?",
            (status, utcnow_iso(), message_id),
        )

    async def assign_run(self, message_id: int, run_id: str, status: str) -> None:
        await self._db.execute(
            "UPDATE messages SET run_id = ?, status = ?, updated_at = ? WHERE id = ?",
            (run_id, status, utcnow_iso(), message_id),
        )

    async def merge_metadata(self, message_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        row = await self.get(message_id)
        merged = {**(row.metadata if row else {}), **patch}
        await self._db.execute(
            "UPDATE messages SET metadata = ?, updated_at = ? WHERE id = ?",
            (json.dumps(merged), utcnow_iso(), message_id),
        )
        return merged

    async def ids_by_status(self, project_id: str, status: str) -> list[int]:
        rows = await self._db.fetchall(
            "SELECT id FROM messages WHERE project_id = ? AND status = ? ORDER BY id",
            (project_id, status),
        )
        return [int(r["id"]) for r in rows]

    async def queued_user_rows(self, project_id: str) -> list[MessageRow]:
        rows = await self._db.fetchall(
            f"SELECT {_COLS} FROM messages WHERE project_id = ? AND role = 'user' "
            "AND status = 'queued' ORDER BY id",
            (project_id,),
        )
        return [_row(r) for r in rows]

    async def projects_with_queued(self) -> list[str]:
        rows = await self._db.fetchall(
            "SELECT DISTINCT project_id FROM messages WHERE role = 'user' AND status = 'queued' "
            "ORDER BY project_id"
        )
        return [str(r["project_id"]) for r in rows]

    async def finalize_streaming(self, project_id: str) -> None:
        """Rows left 'streaming' by a crashed process keep their persisted content."""
        for r in await self._db.fetchall(
            "SELECT id FROM messages WHERE project_id = ? AND status = 'streaming'", (project_id,)
        ):
            await self.merge_metadata(int(r["id"]), {"recovered": True})
            await self.set_status(int(r["id"]), "done")
