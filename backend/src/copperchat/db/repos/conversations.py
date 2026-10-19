"""Conversations ("sessions") of a project (spec §4.2, plan S-C2)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Literal, cast, get_args

import aiosqlite

from copperchat.db import Database, utcnow_iso
from copperchat.errors import ConversationNotFound, ConversationStateConflict, InvalidConversation

ConversationType = Literal["free", "requirements", "schematic", "board"]
ConversationStatus = Literal["open", "closing", "closed"]
CONVERSATION_TYPES: tuple[str, ...] = get_args(ConversationType)
_STATUSES: tuple[str, ...] = get_args(ConversationStatus)
TITLE_MAX = 120
_SEQ_RETRIES = 20


def validate_title(title: str) -> str:
    """1-120 characters, a single line (no control characters)."""
    if not 1 <= len(title) <= TITLE_MAX:
        raise InvalidConversation(f"a session title has 1 to {TITLE_MAX} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in title):
        raise InvalidConversation("a session title is a single line")
    return title


@dataclass(frozen=True)
class ConversationRow:
    id: str
    project_id: str
    seq: int
    title: str
    type: ConversationType
    status: ConversationStatus
    archived: bool
    sdk_session_id: str | None
    docs_seen: dict[str, str]
    created_at: str
    updated_at: str
    last_message_at: str | None
    closed_at: str | None

    def to_wire(self, usage: dict[str, Any] | None = None) -> dict[str, Any]:
        """ConversationDTO (camelCase, spec §6.1)."""
        return {
            "id": self.id,
            "seq": self.seq,
            "title": self.title,
            "type": self.type,
            "status": self.status,
            "archived": self.archived,
            "createdAt": self.created_at,
            "lastMessageAt": self.last_message_at,
            "closedAt": self.closed_at,
            "usage": usage or empty_usage(),
        }


def empty_usage() -> dict[str, Any]:
    return {"inputTokens": 0, "outputTokens": 0, "costUsd": 0}


def _row(r: aiosqlite.Row) -> ConversationRow:
    return ConversationRow(
        id=str(r["id"]),
        project_id=str(r["project_id"]),
        seq=int(r["seq"]),
        title=str(r["title"]),
        type=cast(ConversationType, r["type"]),
        status=cast(ConversationStatus, r["status"]),
        archived=bool(r["archived"]),
        sdk_session_id=r["sdk_session_id"],
        docs_seen=json.loads(r["docs_seen"] or "{}"),
        created_at=str(r["created_at"]),
        updated_at=str(r["updated_at"]),
        last_message_at=r["last_message_at"],
        closed_at=r["closed_at"],
    )


class ConversationRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, project_id: str, type: str, title: str | None) -> ConversationRow:
        if type not in CONVERSATION_TYPES:
            raise InvalidConversation(f"unknown session type: {type}")
        if title is not None:
            validate_title(title)
        for _ in range(_SEQ_RETRIES):
            cid = uuid.uuid4().hex
            now = utcnow_iso()
            try:
                # seq = max + 1 in one statement; a concurrent create that took the same
                # seq fails on UNIQUE(project_id, seq) and retries.
                await self._db.execute(
                    "INSERT INTO conversations (id, project_id, seq, title, type, created_at, "
                    "updated_at) SELECT ?, ?, n, COALESCE(?, 'Session ' || n), ?, ?, ? FROM "
                    "(SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM conversations "
                    "WHERE project_id = ?)",
                    (cid, project_id, title, type, now, now, project_id),
                )
            except sqlite3.IntegrityError:
                continue
            row = await self.get(cid)
            if row is None:
                raise RuntimeError(f"conversation {cid} vanished after insert")
            return row
        raise RuntimeError(f"could not allocate a session number for {project_id}")

    async def get(self, conversation_id: str) -> ConversationRow | None:
        r = await self._db.fetchone("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        return None if r is None else _row(r)

    async def get_for_project(self, project_id: str, conversation_id: str) -> ConversationRow:
        row = await self.get(conversation_id)
        if row is None or row.project_id != project_id:
            raise ConversationNotFound("no such session in this project")
        return row

    async def list(self, project_id: str, archived: bool = False) -> list[ConversationRow]:
        rows = await self._db.fetchall(
            "SELECT * FROM conversations WHERE project_id = ? AND archived = ? "
            "ORDER BY COALESCE(last_message_at, created_at) DESC, seq DESC",
            (project_id, int(archived)),
        )
        return [_row(r) for r in rows]

    async def update(
        self, cid: str, *, title: str | None = None, archived: bool | None = None
    ) -> ConversationRow:
        sets: list[str] = []
        params: list[Any] = []
        if title is not None:
            sets.append("title = ?")
            params.append(validate_title(title))
        if archived is not None:
            sets.append("archived = ?")
            params.append(int(archived))
        if sets:
            await self._db.execute(
                f"UPDATE conversations SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                (*params, utcnow_iso(), cid),
            )
        row = await self.get(cid)
        if row is None:
            raise ConversationNotFound("no such session")
        return row

    async def set_status(self, cid: str, status: str, *, expect: str) -> ConversationRow:
        if status not in _STATUSES or expect not in _STATUSES:
            raise InvalidConversation(f"unknown session status: {status}")
        now = utcnow_iso()
        async with self._db.transaction():
            await self._db.execute(
                "UPDATE conversations SET status = ?, updated_at = ?, "
                "closed_at = CASE WHEN ? = 'closed' THEN ? ELSE NULL END "
                "WHERE id = ? AND status = ?",
                (status, now, status, now, cid, expect),
            )
            changed = await self._db.fetchone("SELECT changes() AS n")
        if changed is None or int(changed["n"]) == 0:
            raise ConversationStateConflict(f"the session is not {expect}")
        row = await self.get(cid)
        if row is None:
            raise ConversationNotFound("no such session")
        return row

    async def set_sdk_session(self, cid: str, sdk_session_id: str) -> None:
        await self._db.execute(
            "UPDATE conversations SET sdk_session_id = ?, updated_at = ? WHERE id = ?",
            (sdk_session_id, utcnow_iso(), cid),
        )

    async def set_docs_seen(self, cid: str, docs_seen: dict[str, str]) -> None:
        await self._db.execute(
            "UPDATE conversations SET docs_seen = ?, updated_at = ? WHERE id = ?",
            (json.dumps(docs_seen, sort_keys=True), utcnow_iso(), cid),
        )

    async def touch(self, cid: str) -> None:
        now = utcnow_iso()
        await self._db.execute(
            "UPDATE conversations SET last_message_at = ?, updated_at = ? WHERE id = ?",
            (now, now, cid),
        )

    async def usage_totals(self, project_id: str) -> dict[str, dict[str, Any]]:
        """cid -> summed ``runs.usage`` (only conversations with runs are listed)."""
        rows = await self._db.fetchall(
            "SELECT conversation_id AS cid, "
            "COALESCE(SUM(json_extract(usage, '$.inputTokens')), 0) AS i, "
            "COALESCE(SUM(json_extract(usage, '$.outputTokens')), 0) AS o, "
            "COALESCE(SUM(json_extract(usage, '$.costUsd')), 0) AS c "
            "FROM runs WHERE project_id = ? AND conversation_id IS NOT NULL "
            "GROUP BY conversation_id",
            (project_id,),
        )
        return {
            str(r["cid"]): {
                "inputTokens": int(r["i"]),
                "outputTokens": int(r["o"]),
                "costUsd": float(r["c"]),
            }
            for r in rows
        }

    async def closing(self) -> list[ConversationRow]:
        rows = await self._db.fetchall(
            "SELECT * FROM conversations WHERE status = 'closing' ORDER BY project_id, seq"
        )
        return [_row(r) for r in rows]
