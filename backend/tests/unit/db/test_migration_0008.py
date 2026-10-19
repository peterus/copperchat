"""0008_conversations: legacy history becomes "Session 1" of each project (spec §4.2)."""

import json
import shutil
from pathlib import Path

import aiosqlite
import pytest_asyncio

from copperchat.db import MIGRATIONS_DIR, Database, migrate

P1, P2, P3 = "aaaaaaaaaaa1", "aaaaaaaaaaa2", "aaaaaaaaaaa3"


async def _raw(path: Path) -> Database:
    conn = await aiosqlite.connect(path, isolation_level=None)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    return Database(conn)


def _legacy_dir(tmp: Path) -> Path:
    d = tmp / "legacy"
    d.mkdir()
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(f.name[:4]) <= 7:
            shutil.copy(f, d / f.name)
    return d


async def _msg(db: Database, pid: str, content: str, at: str, run: str | None = None) -> int:
    return await db.execute(
        "INSERT INTO messages (project_id, run_id, role, kind, content, status, metadata, "
        "created_at, updated_at) VALUES (?, ?, 'user', 'text', ?, 'done', '{}', ?, ?)",
        (pid, run, content, at, at),
    )


async def _run(db: Database, rid: str, pid: str, usage: dict | None = None) -> None:
    await db.execute(
        "INSERT INTO runs (id, project_id, started_at, finished_at, reason, usage) "
        "VALUES (?, ?, '2026-10-01T10:00:00.000Z', '2026-10-01T10:01:00.000Z', 'completed', ?)",
        (rid, pid, json.dumps(usage) if usage else None),
    )


@pytest_asyncio.fixture
async def legacy(tmp_path: Path):
    path = tmp_path / "copperchat.db"
    db = await _raw(path)
    await migrate(db, _legacy_dir(tmp_path))
    ids = [
        await _msg(db, P1, "first", "2026-10-01T10:00:00.000Z", "r1"),
        await _msg(db, P1, "second", "2026-10-01T10:00:05.000Z", "r1"),
        await _msg(db, P1, "third", "2026-10-02T09:00:00.000Z", "r2"),
    ]
    await _run(db, "r1", P1, {"inputTokens": 10, "outputTokens": 2})
    await _run(db, "r2", P1)
    await _run(db, "r3", P2)
    await db.execute(
        "INSERT INTO agent_sessions (project_id, sdk_session_id, updated_at) VALUES (?, ?, ?)",
        (P1, "sdk-1", "2026-10-02T09:00:00.000Z"),
    )
    await db.execute(
        "INSERT INTO agent_sessions (project_id, sdk_session_id, updated_at) VALUES (?, ?, ?)",
        (P3, "sdk-3", "2026-10-02T09:00:00.000Z"),
    )
    await db.execute(
        "INSERT INTO plans (id, project_id, title, summary, status, revision, created_run_id, "
        "created_at, updated_at) VALUES ('pl1', ?, 't', 's', 'proposed', 1, 'r2', ?, ?)",
        (P1, "2026-10-02T09:00:00.000Z", "2026-10-02T09:00:00.000Z"),
    )
    yield db, ids
    await db.close()


async def _convs(db: Database) -> dict[str, dict]:
    rows = await db.fetchall("SELECT * FROM conversations")
    return {r["project_id"]: dict(r) for r in rows}


async def test_one_session_per_project_with_history(legacy) -> None:
    db, _ = legacy
    await migrate(db)
    convs = await _convs(db)
    assert set(convs) == {P1, P2, P3}
    for c in convs.values():
        assert (c["seq"], c["title"], c["type"], c["status"], c["archived"]) == (
            1,
            "Session 1",
            "free",
            "open",
            0,
        )
        assert len(c["id"]) == 32 and int(c["id"], 16) >= 0
        assert c["created_at"].endswith("Z") and c["updated_at"].endswith("Z")
        assert c["docs_seen"] == "{}"
    assert convs[P1]["sdk_session_id"] == "sdk-1"
    assert convs[P3]["sdk_session_id"] == "sdk-3"
    assert convs[P2]["sdk_session_id"] is None
    assert convs[P1]["created_at"] == "2026-10-01T10:00:00.000Z"
    assert convs[P1]["last_message_at"] == "2026-10-02T09:00:00.000Z"
    assert convs[P2]["last_message_at"] is None


async def test_every_row_is_linked_and_ids_unchanged(legacy) -> None:
    db, ids = legacy
    await migrate(db)
    convs = await _convs(db)
    for table in ("messages", "runs", "plans"):
        nulls = await db.fetchone(f"SELECT COUNT(*) AS n FROM {table} WHERE conversation_id IS NULL")
        assert nulls is not None and nulls["n"] == 0, table
        rows = await db.fetchall(f"SELECT project_id, conversation_id FROM {table}")
        assert all(r["conversation_id"] == convs[r["project_id"]]["id"] for r in rows), table
    msgs = await db.fetchall("SELECT id, content FROM messages ORDER BY id")
    assert [(r["id"], r["content"]) for r in msgs] == list(zip(ids, ["first", "second", "third"], strict=True))
    counts = {
        t: (await db.fetchone(f"SELECT COUNT(*) AS n FROM {t}"))["n"]  # type: ignore[index]
        for t in ("messages", "runs", "plans")
    }
    assert counts == {"messages": 3, "runs": 3, "plans": 1}


async def test_agent_sessions_dropped_and_rerun_is_noop(legacy) -> None:
    db, _ = legacy
    await migrate(db)
    tables = {r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master")}
    assert "agent_sessions" not in tables
    assert {"conversations", "ix_conversations_project", "ix_messages_conversation"} <= tables
    before = await _convs(db)
    await migrate(db)
    assert await _convs(db) == before
    fk = await db.fetchall("PRAGMA foreign_key_check")
    assert fk == []


async def test_fresh_database_has_no_conversations(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "fresh.db")
    try:
        assert await db.fetchall("SELECT * FROM conversations") == []
    finally:
        await db.close()
