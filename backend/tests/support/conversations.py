"""A conversation row for tests that call a backend's ``start`` directly."""

from __future__ import annotations

from typing import Any

from copperchat.db.repos.conversations import ConversationRow

TEST_CID = "c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0"


def conversation_row(
    project_id: str = "a1b2c3d4e5f6", cid: str = TEST_CID, type: str = "free"
) -> ConversationRow:
    return ConversationRow(
        id=cid,
        project_id=project_id,
        seq=1,
        title="Session 1",
        type=type,  # type: ignore[arg-type]
        status="open",
        archived=False,
        sdk_session_id=None,
        docs_seen={},
        created_at="2026-01-01T00:00:00.000Z",
        updated_at="2026-01-01T00:00:00.000Z",
        last_message_at=None,
        closed_at=None,
    )


async def ensure_test_conversation(db: Any, project_id: str = "a1b2c3d4e5f6") -> str:
    """Insert the TEST_CID conversation (rows that reference it need it: foreign keys)."""
    await db.execute(
        "INSERT OR IGNORE INTO conversations (id, project_id, seq, title, type, created_at, "
        "updated_at) VALUES (?, ?, 1000, 'Default', 'free', ?, ?)",
        (TEST_CID, project_id, "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z"),
    )
    return TEST_CID
