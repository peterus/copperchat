from datetime import UTC, datetime

import pytest_asyncio

from copperchat.db import Database
from copperchat.db.repos.agent_state import AgentSessionRepo, RateLimitRepo, RunRepo


@pytest_asyncio.fixture
async def db(tmp_path):
    d = await Database.open(tmp_path / "t.db")
    yield d
    await d.close()


async def test_chat_tables_exist(db):
    rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type IN ('table','index')")
    names = {r["name"] for r in rows}
    assert {"messages", "runs", "agent_sessions", "rate_limit_state", "ux_messages_client"} <= names


async def test_run_start_and_finish(db):
    runs = RunRepo(db)
    await runs.start("r1", "a1b2c3d4e5f6")
    await runs.finish("r1", "completed", {"inputTokens": 3})
    run = await runs.get("r1")
    assert run is not None
    assert run["reason"] == "completed"
    assert run["usage"] == {"inputTokens": 3}
    assert run["finished_at"] is not None


async def test_finalize_open_marks_crashed_runs_interrupted(db):
    runs = RunRepo(db)
    await runs.start("r1", "a1b2c3d4e5f6")
    await runs.start("r2", "a1b2c3d4e5f6")
    await runs.finish("r2", "completed", None)
    await runs.start("r3", "ffffffffffff")
    await runs.finalize_open("a1b2c3d4e5f6")
    r1, r2, r3 = await runs.get("r1"), await runs.get("r2"), await runs.get("r3")
    assert r1 is not None and r1["finished_at"] is not None and r1["reason"] == "interrupted"
    assert r2 is not None and r2["reason"] == "completed"
    assert r3 is not None and r3["finished_at"] is None  # other project untouched


async def test_agent_session_upsert(db):
    repo = AgentSessionRepo(db)
    assert await repo.get("p") is None
    await repo.set("p", "s1")
    await repo.set("p", "s2")
    assert await repo.get("p") == "s2"


async def test_rate_limit_roundtrip_and_clear(db):
    repo = RateLimitRepo(db)
    when = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    await repo.set("p", when)
    assert await repo.get("p") == when
    await repo.set("p", None)
    assert await repo.get("p") is None
