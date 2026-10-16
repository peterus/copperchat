import asyncio
from pathlib import Path

import pytest

from copperchat.db import Database, list_migrations, migrate


async def test_open_applies_core_migration(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "x.db")
    try:
        tables = {
            r["name"]
            for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"users", "projects", "schema_migrations"} <= tables
        row = await db.fetchone("PRAGMA user_version")
        assert row is not None and row[0] >= 1
        fk = await db.fetchone("PRAGMA foreign_keys")
        assert fk is not None and fk[0] == 1
        mode = await db.fetchone("PRAGMA journal_mode")
        assert mode is not None and mode[0] == "wal"
    finally:
        await db.close()


async def test_reopen_is_idempotent(tmp_path: Path) -> None:
    for _ in range(2):
        db = await Database.open(tmp_path / "x.db")
        await db.close()


async def test_execute_returns_lastrowid_and_fetch(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "x.db")
    try:
        rid = await db.execute(
            "INSERT INTO users(username, password_hash, created_at) VALUES (?,?,?)",
            ("a", "h", "2026-10-15T00:00:00Z"),
        )
        assert rid == 1
        row = await db.fetchone("SELECT username FROM users WHERE id = :id", {"id": rid})
        assert row is not None and row["username"] == "a"
    finally:
        await db.close()


async def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "x.db")
    try:
        with pytest.raises(RuntimeError):
            async with db.transaction():
                await db.execute(
                    "INSERT INTO projects(id, rel_path, name) VALUES ('p','a.kicad_pro','a')"
                )
                raise RuntimeError("boom")
        assert await db.fetchall("SELECT * FROM projects") == []
    finally:
        await db.close()


async def test_transactions_are_serialised(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "x.db")
    try:

        async def writer(i: int) -> None:
            async with db.transaction():
                await db.execute(
                    "INSERT INTO projects(id, rel_path, name) VALUES (?,?,?)",
                    (f"p{i}", f"{i}.kicad_pro", str(i)),
                )
                await asyncio.sleep(0)

        await asyncio.gather(*(writer(i) for i in range(10)))
        rows = await db.fetchall("SELECT id FROM projects")
        assert len(rows) == 10
    finally:
        await db.close()


async def test_out_of_order_migration_is_still_applied(tmp_path: Path) -> None:
    mig = tmp_path / "migs"
    mig.mkdir()
    (mig / "0001_a.sql").write_text("CREATE TABLE a (x INTEGER);")
    (mig / "0004_d.sql").write_text("CREATE TABLE d (x INTEGER);")
    db = await Database.open(tmp_path / "x.db")
    try:
        await migrate(db, mig)
        (mig / "0003_c.sql").write_text("CREATE TABLE c (x INTEGER);")
        await migrate(db, mig)
        names = {r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master")}
        # 0001 is already applied by Database.open (real 0001_core.sql), so the
        # test's 0001_a.sql is skipped as "version 1 already applied".
        assert {"c", "d"} <= names and "a" not in names
        uv = await db.fetchone("PRAGMA user_version")
        assert uv is not None and uv[0] == 4
    finally:
        await db.close()


def test_list_migrations_rejects_duplicates(tmp_path: Path) -> None:
    (tmp_path / "0002_x.sql").write_text("")
    (tmp_path / "0002_y.sql").write_text("")
    with pytest.raises(RuntimeError, match="duplicate"):
        list_migrations(tmp_path)


async def test_reads_do_not_see_another_tasks_uncommitted_writes(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "x.db")
    try:
        inside = asyncio.Event()

        async def writer() -> None:
            with pytest.raises(RuntimeError):
                async with db.transaction():
                    await db.execute(
                        "INSERT INTO projects(id, rel_path, name) VALUES ('p','a.kicad_pro','a')"
                    )
                    inside.set()
                    await asyncio.sleep(0.05)
                    raise RuntimeError("rolled back")

        task = asyncio.create_task(writer())
        await inside.wait()
        assert await db.fetchall("SELECT * FROM projects") == []
        await task
    finally:
        await db.close()
