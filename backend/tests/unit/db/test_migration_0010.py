"""0010_plan_step_resume (#13, spec §9.2): plan_steps is rebuilt for the 'paused' step
status and the step counters; existing plans and steps survive unchanged."""

import shutil
from pathlib import Path

import aiosqlite
import pytest

from copperchat.db import MIGRATIONS_DIR, Database, migrate
from copperchat.plans.repo import PlanRepo
from tests.support.conversations import TEST_CID, ensure_test_conversation

PID = "a1b2c3d4e5f6"
PLAN = "f" * 32
NOW = "2026-10-20T10:00:00.000Z"


async def _raw(path: Path) -> Database:
    conn = await aiosqlite.connect(path, isolation_level=None)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    return Database(conn)


def _upto(tmp: Path, last: int) -> Path:
    d = tmp / f"upto{last}"
    d.mkdir()
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(f.name[:4]) <= last:
            shutil.copy(f, d / f.name)
    return d


async def test_existing_steps_survive_and_paused_is_allowed(tmp_path: Path) -> None:
    db = await _raw(tmp_path / "copperchat.db")
    try:
        await _check(db, tmp_path)
    finally:
        await db.close()


async def _check(db: Database, tmp_path: Path) -> None:
    await migrate(db, _upto(tmp_path, 9))
    await ensure_test_conversation(db, PID)
    await db.execute(
        "INSERT INTO plans(id, project_id, title, summary, status, revision, event_seq,"
        " pause_reason, base_snapshot_id, created_run_id, conversation_id, created_at,"
        " updated_at) VALUES (?, ?, 't', 's', 'paused', 2, 1, 'turn_ended', NULL, 'r1',"
        " ?, ?, ?)",
        (PLAN, PID, TEST_CID, NOW, NOW),
    )
    for idx, status in ((1, "done"), (2, "interrupted")):
        await db.execute(
            "INSERT INTO plan_steps(id, plan_id, idx, title, intent, expected_changes,"
            " touches, verification, status, change_count) VALUES (?, ?, ?, 'st', 'i', 4,"
            " '{\"refs\": [], \"nets\": []}', 'v', ?, 2)",
            (f"{idx}" * 32, PLAN, idx, status),
        )
    for i, (run, step) in enumerate((("r0", 2), ("r1", 2), ("r1", 1)), start=1):
        await db.execute(
            "INSERT INTO changes(id, project_id, run_id, plan_step_id, call_seq, tool, op,"
            " kind, uuid, sheet_path, created_at) VALUES (?, ?, ?, ?, 1, 't', 'add', 'wire',"
            " 'u', '/', ?)",
            (10 * i, PID, run, f"{step}" * 32, NOW),
        )
    await db.execute("UPDATE plan_steps SET run_id = 'r1'")
    await migrate(db)
    plan = await PlanRepo(db).get(PID, PLAN)
    # the unfinished step keeps counting its last run's rows; the index exists
    assert [s.change_base for s in plan.steps] == [0, 19]
    assert await db.fetchone("SELECT name FROM sqlite_master WHERE name = 'ix_changes_step'")
    assert [(s.idx, s.status, s.change_count, s.used, s.placed) for s in plan.steps] == [
        (1, "done", 2, 0, ()),
        (2, "interrupted", 2, 0, ()),
    ]
    assert plan.revise_reason is None and plan.previous is None
    await db.execute("UPDATE plan_steps SET status = 'paused' WHERE idx = 2")
    with pytest.raises(Exception, match="CHECK"):
        await db.execute("UPDATE plan_steps SET status = 'bogus' WHERE idx = 2")
    # the cascade from plans still holds after the rebuild
    await db.execute("DELETE FROM plans WHERE id = ?", (PLAN,))
    assert await db.fetchall("SELECT id FROM plan_steps") == []
