"""Migration 0006 (editing tables), EditingSettingsRepo and ChangeRepo."""

import shutil
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from copperchat.changes.repo import ChangeEntry, ChangeRepo
from copperchat.db import MIGRATIONS_DIR, Database, migrate
from copperchat.db.repos.editing_settings import (
    EditingSettings,
    EditingSettingsRepo,
    InvalidEditingSettings,
)

NOW = "2026-10-18T00:00:00.000Z"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.open(tmp_path / "x.db")
    try:
        yield database
    finally:
        await database.close()


async def _columns(db: Database, table: str) -> set[str]:
    return {str(r["name"]) for r in await db.fetchall(f"PRAGMA table_info({table})")}


async def test_fresh_db_has_editing_tables(db: Database) -> None:
    tables = {
        str(r["name"])
        for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"editing_settings", "changes", "erc_results", "plans", "plan_steps"} <= tables
    assert {"kind", "plan_step_id"} <= await _columns(db, "history_snapshots")
    row = await db.fetchone("PRAGMA user_version")
    assert row is not None and row[0] >= 6


async def test_migration_applies_on_version_5_db(tmp_path: Path) -> None:
    old = tmp_path / "old_migrations"
    old.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("000[1-5]_*.sql")):
        shutil.copy(path, old / path.name)
    conn = await aiosqlite.connect(tmp_path / "x.db", isolation_level=None)
    conn.row_factory = aiosqlite.Row
    db = Database(conn)
    try:
        await migrate(db, old)
        row = await db.fetchone("PRAGMA user_version")
        assert row is not None and row[0] == 5
        await db.execute(
            "INSERT INTO history_snapshots(id, project_id, run_id, created_at, label, files) "
            "VALUES ('s1', 'p', 'r', ?, 'l', '[]')",
            (NOW,),
        )
        await migrate(db)
        snap = await db.fetchone("SELECT kind, plan_step_id FROM history_snapshots")
        assert snap is not None
        assert (snap["kind"], snap["plan_step_id"]) == ("turn", None)
        row = await db.fetchone("PRAGMA user_version")
        assert row is not None and row[0] >= 6
    finally:
        await db.close()


async def test_history_kind_defaults_to_turn(db: Database) -> None:
    await db.execute(
        "INSERT INTO history_snapshots(id, project_id, run_id, created_at, label, files) "
        "VALUES ('s1', 'p', 'r', ?, 'l', '[]')",
        (NOW,),
    )
    row = await db.fetchone("SELECT kind FROM history_snapshots WHERE id = 's1'")
    assert row is not None and row["kind"] == "turn"
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO history_snapshots(id, project_id, run_id, created_at, label, files, kind)"
            " VALUES ('s2', 'p', 'r', ?, 'l', '[]', 'bogus')",
            (NOW,),
        )


async def _plan(db: Database, plan_id: str, project_id: str, status: str) -> None:
    await db.execute(
        "INSERT INTO plans(id, project_id, title, summary, status, revision, created_run_id, "
        "created_at, updated_at) VALUES (?, ?, 't', 's', ?, 1, 'r', ?, ?)",
        (plan_id, project_id, status, NOW, NOW),
    )


async def test_ux_plans_open_allows_one_open_plan_per_project(db: Database) -> None:
    await _plan(db, "a", "p1", "running")
    with pytest.raises(sqlite3.IntegrityError):
        await _plan(db, "b", "p1", "proposed")
    await _plan(db, "c", "p1", "done")
    await _plan(db, "d", "p1", "rejected")
    await _plan(db, "e", "p2", "paused")
    with pytest.raises(sqlite3.IntegrityError):
        await _plan(db, "f", "p3", "bogus")


async def test_plan_steps_unique_idx_and_cascade(db: Database) -> None:
    await _plan(db, "a", "p1", "proposed")
    sql = (
        "INSERT INTO plan_steps(id, plan_id, idx, title, intent, expected_changes, touches, "
        "verification, status) VALUES (?, 'a', ?, 't', 'i', 3, '{}', 'v', ?)"
    )
    await db.execute(sql, ("s1", 0, "pending"))
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(sql, ("s2", 0, "pending"))
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(sql, ("s3", 1, "bogus"))
    await db.execute("DELETE FROM plans WHERE id = 'a'")
    assert await db.fetchall("SELECT * FROM plan_steps") == []


# --- EditingSettingsRepo ---------------------------------------------------------------


async def test_settings_defaults(db: Database) -> None:
    s = await EditingSettingsRepo(db).get("p1")
    assert s == EditingSettings()
    assert (s.plan_policy, s.change_budget, s.annotation_block, s.show_agent_view) == (
        "large",
        20,
        100,
        True,
    )


async def test_settings_partial_update_persists(db: Database) -> None:
    repo = EditingSettingsRepo(db)
    s = await repo.put("p1", change_budget=50)
    assert s == EditingSettings(change_budget=50)
    s = await repo.put("p1", plan_policy="never", show_agent_view=False)
    assert s == EditingSettings(plan_policy="never", change_budget=50, show_agent_view=False)
    assert await repo.get("p1") == s
    assert await repo.get("p2") == EditingSettings()
    s = await repo.put("p1", annotation_block=1000)
    assert s.annotation_block == 1000 and s.change_budget == 50


@pytest.mark.parametrize(
    "fields",
    [
        {"plan_policy": "sometimes"},
        {"plan_policy": 1},
        {"change_budget": 0},
        {"change_budget": 501},
        {"change_budget": True},
        {"change_budget": "20"},
        {"change_budget": 2.5},
        {"annotation_block": 10},
        {"annotation_block": True},
        {"show_agent_view": 1},
        {"show_agent_view": "yes"},
        {"bogus": 1},
    ],
)
async def test_settings_validation(db: Database, fields: dict[str, Any]) -> None:
    repo = EditingSettingsRepo(db)
    with pytest.raises(InvalidEditingSettings) as info:
        await repo.put("p1", **fields)
    assert info.value.code == "invalid_argument"
    assert info.value.http_status == 400
    assert await repo.get("p1") == EditingSettings()


async def test_settings_bounds_accepted(db: Database) -> None:
    repo = EditingSettingsRepo(db)
    assert (await repo.put("p1", change_budget=1)).change_budget == 1
    assert (await repo.put("p1", change_budget=500)).change_budget == 500
    assert (await repo.put("p1", plan_policy="always")).plan_policy == "always"


# --- ChangeRepo -------------------------------------------------------------------------


def _entry(run_id: str, seq: int, **kw: Any) -> ChangeEntry:
    base: dict[str, Any] = {
        "project_id": "p1",
        "run_id": run_id,
        "plan_step_id": None,
        "call_seq": seq,
        "tool": "sch_add_symbol",
        "op": "add",
        "kind": "symbol",
        "uuid": f"u{run_id}{seq}",
        "sheet_path": "/",
    }
    base.update(kw)
    return ChangeEntry(**base)


async def test_change_repo_round_trip(db: Database) -> None:
    repo = ChangeRepo(db)
    count = await repo.add_many(
        [
            _entry("r1", 1, ref="R1", after={"value": "10k"}, plan_step_id="s1"),
            _entry(
                "r1",
                2,
                op="modify",
                net="GND",
                before={"a": 1},
                after={"a": 2},
                plan_step_id="s2",
                side_effect=True,
            ),
            _entry("r1", 3, op="delete", before={"x": [1, 2]}, plan_step_id="s1"),
        ]
    )
    assert count == 3
    got = await repo.for_run("p1", "r1")
    assert [e.call_seq for e in got] == [1, 2, 3]
    first = got[0]
    assert first.id is not None and first.created_at is not None
    assert (first.ref, first.net, first.before, first.after) == ("R1", None, None, {"value": "10k"})
    assert first.side_effect is False
    assert got[1].side_effect is True and got[1].before == {"a": 1}
    assert got[2].before == {"x": [1, 2]} and got[2].after is None

    only_s1 = await repo.for_run("p1", "r1", step_id="s1")
    assert [e.call_seq for e in only_s1] == [1, 3]
    assert await repo.for_run("p2", "r1") == []


async def test_change_repo_add_many_empty_and_explicit_created_at(db: Database) -> None:
    repo = ChangeRepo(db)
    assert await repo.add_many([]) == 0
    await repo.add_many([_entry("r1", 1, created_at=NOW)])
    (e,) = await repo.for_run("p1", "r1")
    assert e.created_at == NOW


async def test_change_repo_runs(db: Database) -> None:
    repo = ChangeRepo(db)
    await repo.add_many(
        [
            _entry("r1", 1, created_at="2026-10-18T00:00:01.000Z"),
            _entry("r1", 2, side_effect=True, created_at="2026-10-18T00:00:02.000Z"),
        ]
    )
    await repo.add_many(
        [
            _entry("r2", 1, plan_step_id="s2", created_at="2026-10-18T00:01:00.000Z"),
            _entry("r2", 2, plan_step_id="s1"),
            _entry("r2", 3, plan_step_id="s2"),
            _entry("r2", 4),
        ]
    )
    await repo.add_many([_entry("r9", 1, project_id="p2")])
    runs = await repo.runs("p1")
    assert runs == [
        {
            "run_id": "r2",
            "step_ids": ["s2", "s1"],
            "count": 4,
            "side_effects": 0,
            "created_at": "2026-10-18T00:01:00.000Z",
        },
        {
            "run_id": "r1",
            "step_ids": [],
            "count": 2,
            "side_effects": 1,
            "created_at": "2026-10-18T00:00:01.000Z",
        },
    ]
    assert [r["run_id"] for r in await repo.runs("p1", limit=1)] == ["r2"]
