"""Upgrade acceptance for 0008 (Review Focus 2): a realistic v0.2.0-shaped database with
data in every table upgrades with no lost row, every row linked to its project's
"Session 1", and the first burst after the upgrade resumes the old SDK session.

``COPPERCHAT_UPGRADE_DB=/path/to/copy/of/copperchat.db`` additionally runs the same checks
on a copy of a real database (the file is copied first; the given path is only read).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import aiosqlite
import pytest
from pydantic import SecretStr

from copperchat.agent.broadcaster import Broadcaster
from copperchat.agent.engine import RunEngine
from copperchat.agent.fake import FakeAgent
from copperchat.config import Settings
from copperchat.db import MIGRATIONS_DIR, Database, migrate
from copperchat.db.repos.conversations import ConversationRepo
from copperchat.projects import Project

P1, P2, P3, P4 = "aaaaaaaaaaa1", "aaaaaaaaaaa2", "aaaaaaaaaaa3", "aaaaaaaaaaa4"
_KEPT = (
    "users",
    "projects",
    "auth_sessions",
    "history_snapshots",
    "document_hashes",
    "messages",
    "runs",
    "rate_limit_state",
    "skill_prefs",
    "uploads",
    "editing_settings",
    "changes",
    "erc_results",
    "plans",
    "plan_steps",
    "push_subscriptions",
)


def _ts(day: int, sec: int = 0) -> str:
    return f"2026-10-{day:02d}T10:{sec // 60:02d}:{sec % 60:02d}.000Z"


async def _raw(path: Path) -> Database:
    conn = await aiosqlite.connect(path, isolation_level=None)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    return Database(conn)


async def _build_v020(path: Path, tmp: Path) -> None:
    legacy = tmp / "legacy-migrations"
    legacy.mkdir()
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(f.name[:4]) <= 7:
            shutil.copy(f, legacy / f.name)
    db = await _raw(path)
    await migrate(db, legacy)
    x = db.execute
    await x("INSERT INTO users VALUES (1, 'admin', 'hash', ?)", (_ts(1),))
    await x("INSERT INTO auth_sessions VALUES ('s1', 1, ?)", (_ts(1),))
    for pid, name in ((P1, "ecc83"), (P2, "ldo"), (P3, "mcu"), (P4, "idle")):
        await x(
            "INSERT INTO projects VALUES (?, ?, ?, ?)",
            (pid, f"{name}/{name}.kicad_pro", name, _ts(2)),
        )
    # P1: a long chat over three days and 6 runs, with tool rows, a sub-agent row, a
    # queued message left by a restart, an SDK session, plans and history.
    mid = 0
    for run in range(6):
        rid = f"run-p1-{run}"
        usage = {"inputTokens": 100 + run, "outputTokens": 10, "costUsd": 0.01}
        await x(
            "INSERT INTO runs VALUES (?, ?, ?, ?, 'completed', ?)",
            (rid, P1, _ts(3 + run // 3, run), _ts(3 + run // 3, run + 1), json.dumps(usage)),
        )
        for role, kind, content, meta in (
            ("user", "text", f"question {run}", {}),
            ("assistant", "thinking", "hmm", {}),
            ("assistant", "tool_use", "sch_get", {"toolUseId": f"t{run}", "name": "sch_get"}),
            ("assistant", "tool_result", "{}", {"toolUseId": f"t{run}", "isError": False}),
            ("assistant", "text", f"answer {run}", {}),
            ("assistant", "text", "sub", {"parentToolUseId": "x"}),
            ("system", "notice", "ERC after this turn: 0 errors", {"action": "erc-delta"}),
        ):
            mid += 1
            await x(
                "INSERT INTO messages (project_id, client_message_id, run_id, role, kind, "
                "content, status, metadata, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'done', ?, ?, ?)",
                (
                    P1,
                    f"c-{run}" if role == "user" else None,
                    rid,
                    role,
                    kind,
                    content,
                    json.dumps(meta),
                    _ts(3 + run // 3, mid),
                    _ts(3 + run // 3, mid),
                ),
            )
    await x(
        "INSERT INTO messages (project_id, client_message_id, run_id, role, kind, content, "
        "status, metadata, created_at, updated_at) VALUES (?, 'c-left', NULL, 'user', 'text', "
        "'left over', 'queued', '{}', ?, ?)",
        (P1, _ts(6, 1), _ts(6, 1)),
    )
    await x("INSERT INTO agent_sessions VALUES (?, 'sdk-p1', ?)", (P1, _ts(6)))
    for i, run in enumerate(("run-p1-1", "run-p1-4")):
        await x(
            "INSERT INTO history_snapshots (id, project_id, run_id, created_at, label, files, "
            "kind, plan_step_id) VALUES (?, ?, ?, ?, 'turn', '[\"ecc83.kicad_sch\"]', 'turn', "
            "NULL)",
            (f"{i:032x}", P1, run, _ts(4, i)),
        )
    await x("INSERT INTO document_hashes VALUES (?, 'ecc83.kicad_sch', 'abc')", (P1,))
    await x("INSERT INTO editing_settings (project_id) VALUES (?)", (P1,))
    await x("INSERT INTO skill_prefs VALUES (?, 'kicad-schematic', 1)", (P1,))
    await x(
        "INSERT INTO uploads VALUES ('u1', ?, 'image', 'a.png', 'uploads/a.png', 'image/png', "
        "3, ?)",
        (P1, _ts(3)),
    )
    for seq in range(3):
        await x(
            "INSERT INTO changes (project_id, run_id, plan_step_id, call_seq, tool, op, kind, "
            "uuid, sheet_path, created_at) VALUES (?, 'run-p1-4', 'st1', ?, 'sch_add', 'add', "
            "'symbol', ?, '/', ?)",
            (P1, seq, f"u{seq}", _ts(5, seq)),
        )
    await x(
        "INSERT INTO erc_results VALUES ('e1', ?, 'run-p1-4', 'st1', NULL, '[]', '[]', '[]', "
        "'{}', ?)",
        (P1, _ts(5)),
    )
    for plan_id, status in (("pl-done", "done"), ("pl-open", "paused")):
        await x(
            "INSERT INTO plans (id, project_id, title, summary, status, revision, event_seq, "
            "created_run_id, created_at, updated_at) VALUES (?, ?, 't', 's', ?, 1, 0, "
            "'run-p1-3', ?, ?)",
            (plan_id, P1, status, _ts(5), _ts(5)),
        )
        await x(
            "INSERT INTO plan_steps (id, plan_id, idx, title, intent, expected_changes, touches, "
            "verification, status) VALUES (?, ?, 1, 'a', 'b', 1, '{}', 'c', 'done')",
            (f"{plan_id}-1", plan_id),
        )
    # P2: runs and a parked rate limit, and an SDK session, but no messages at all.
    await x("INSERT INTO runs VALUES ('run-p2', ?, ?, NULL, NULL, NULL)", (P2, _ts(7)))
    await x("INSERT INTO agent_sessions VALUES (?, 'sdk-p2', ?)", (P2, _ts(7)))
    await x("INSERT INTO rate_limit_state VALUES (?, ?)", (P2, "2026-10-08T00:00:00+00:00"))
    # P3: a short chat without an SDK session. P4: opened, never chatted.
    for i in range(2):
        await x(
            "INSERT INTO messages (project_id, role, kind, content, status, metadata, "
            "created_at, updated_at) VALUES (?, 'system', 'error', 'no credentials', 'done', "
            "'{}', ?, ?)",
            (P3, _ts(8, i), _ts(8, i)),
        )
    await x(
        "INSERT INTO push_subscriptions VALUES (1, 'https://push.example/1', 'k', 'a', ?)",
        (_ts(2),),
    )
    await db.close()


async def _counts(db: Database) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in _KEPT:
        row = await db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")
        assert row is not None
        out[table] = int(row["n"])
    return out


async def _history(db: Database) -> list[tuple[Any, ...]]:
    rows = await db.fetchall(
        "SELECT id, project_id, run_id, role, kind, content, status FROM messages ORDER BY id"
    )
    return [tuple(r) for r in rows]


async def _check_upgrade(path: Path) -> tuple[Database, dict[str, int]]:
    """Migrate ``path`` and assert the 0008 invariants; returns the open database."""
    db = await _raw(path)
    before, history = await _counts(db), await _history(db)
    projects = {
        str(r["project_id"])
        for r in await db.fetchall(
            "SELECT project_id FROM messages UNION SELECT project_id FROM runs "
            "UNION SELECT project_id FROM agent_sessions"
        )
    }
    sessions = {
        str(r["project_id"]): str(r["sdk_session_id"])
        for r in await db.fetchall("SELECT * FROM agent_sessions")
    }
    await migrate(db)
    assert await _counts(db) == before, "no row lost"
    assert await _history(db) == history, "message ids, order and content unchanged"
    convs = {
        str(r["project_id"]): dict(r) for r in await db.fetchall("SELECT * FROM conversations")
    }
    assert set(convs) == projects, "one conversation per project with history"
    assert all(
        (c["seq"], c["title"], c["type"]) == (1, "Session 1", "free") for c in convs.values()
    )
    assert {p: c["sdk_session_id"] for p, c in convs.items() if c["sdk_session_id"]} == sessions
    for table in ("messages", "runs", "plans"):
        bad = await db.fetchone(
            f"SELECT COUNT(*) AS n FROM {table} t LEFT JOIN conversations c "
            "ON c.id = t.conversation_id WHERE c.id IS NULL OR c.project_id != t.project_id"
        )
        assert bad is not None and bad["n"] == 0, f"{table}: every row in its project's session"
    assert await db.fetchall("PRAGMA foreign_key_check") == []
    integrity = await db.fetchone("PRAGMA integrity_check")
    assert integrity is not None and integrity[0] == "ok"
    tables = {r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master")}
    assert "agent_sessions" not in tables
    await migrate(db)  # idempotent
    assert await _counts(db) == before
    return db, before


async def test_realistic_v020_database_upgrades_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "copperchat.db"
    await _build_v020(path, tmp_path)
    db, counts = await _check_upgrade(path)
    try:
        assert counts["messages"] == 6 * 7 + 1 + 2 and counts["runs"] == 7
        convs = {
            r.project_id: r
            for r in [
                *await ConversationRepo(db).list(P1),
                *await ConversationRepo(db).list(P2),
                *await ConversationRepo(db).list(P3),
            ]
        }
        assert P4 not in convs and await ConversationRepo(db).list(P4) == []
        usage = await ConversationRepo(db).usage_totals(P1)
        assert usage[convs[P1].id] == {
            "inputTokens": sum(100 + r for r in range(6)),
            "outputTokens": 60,
            "costUsd": pytest.approx(0.06),
        }
        assert convs[P1].created_at == _ts(3, 1) and convs[P1].last_message_at == _ts(6, 1)
        # The open plan belongs to Session 1 (it keeps receiving Approve/Resume).
        plan = await db.fetchone("SELECT conversation_id FROM plans WHERE id = 'pl-open'")
        assert plan is not None and plan["conversation_id"] == convs[P1].id
    finally:
        await db.close()


async def test_first_burst_after_upgrade_resumes_the_old_sdk_session(tmp_path: Path) -> None:
    path = tmp_path / "copperchat.db"
    await _build_v020(path, tmp_path)
    db = await Database.open(path)  # the app's own open: runs 0008
    d = tmp_path / "ecc83"
    d.mkdir()
    project = Project(
        id=P1,
        name="ecc83",
        rel_path="ecc83/ecc83.kicad_pro",
        dir=d,
        pro_file=d / "ecc83.kicad_pro",
        sch_file=None,
        pcb_file=None,
    )
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        projects_root=tmp_path,
        data_dir=tmp_path / "data",
        claude_code_oauth_token=SecretStr("t"),
    )
    box: dict[str, FakeAgent] = {}

    def factory(ctx: Any, turn: Any) -> FakeAgent:
        box["agent"] = FakeAgent(ctx_factory=ctx, turn_context=turn)
        return box["agent"]

    engine = RunEngine(
        project=project,
        settings=settings,
        db=db,
        broadcaster=Broadcaster(),
        sessions=cast(Any, SimpleNamespace(current=lambda: None)),
        backend_factory=factory,
    )
    try:
        await engine.start()  # re-drives the queued "left over" row
        agent = box["agent"]
        for _ in range(400):
            if agent.prompts and not engine.is_busy:
                break
            await asyncio.sleep(0.005)
        [conv] = await ConversationRepo(db).list(P1)
        assert [p.text for p in agent.prompts] == ["left over"]
        assert agent.resume_ids == ["sdk-p1"], "the migrated session is resumed"
        assert agent.conversations == [conv.id]
    finally:
        await engine.close()
        await db.close()


@pytest.mark.skipif(
    not os.environ.get("COPPERCHAT_UPGRADE_DB"),
    reason="set COPPERCHAT_UPGRADE_DB to a copy of a real copperchat.db",
)
async def test_copy_of_a_real_database_upgrades_cleanly(tmp_path: Path) -> None:
    source = Path(os.environ["COPPERCHAT_UPGRADE_DB"])
    copy = tmp_path / "copperchat.db"
    # sqlite3's backup API reads the source (including a WAL) without writing to it.
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(copy)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    db, counts = await _check_upgrade(copy)
    await db.close()
    print(f"upgraded a copy of {source}: {counts}")
