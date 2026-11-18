"""0012_footprints (FP-C7): document_hashes stat columns, blob refs, the footprint write
journal (FootprintWriteRepo) and footprint_reviews."""

import shutil
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest

from copperchat.db import MIGRATIONS_DIR, Database, migrate
from copperchat.db.repos.footprints import ABSENT, FootprintWriteRepo

P1 = "a1b2c3d4e5f6"
P2 = "0f9e8d7c6b5a"
SHA_A = "a" * 64
SHA_B = "b" * 64
FILE = "footprints/X.pretty/R_0603.kicad_mod"


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


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.open(tmp_path / "x.db")
    try:
        yield database
    finally:
        await database.close()


async def test_migration_on_0011_keeps_hashes(tmp_path: Path) -> None:
    db = await _raw(tmp_path / "copperchat.db")
    try:
        await migrate(db, _upto(tmp_path, 11))
        await db.execute(
            "INSERT INTO document_hashes(project_id, rel_path, sha256) VALUES (?, ?, ?)",
            (P1, "main.kicad_sch", SHA_A),
        )
        await migrate(db)
        row = await db.fetchone("SELECT * FROM document_hashes")
        assert row is not None
        assert (row["project_id"], row["rel_path"], row["sha256"]) == (
            P1,
            "main.kicad_sch",
            SHA_A,
        )
        assert row["size"] is None and row["mtime_ns"] is None and row["inode"] is None
        tables = {
            str(r["name"])
            for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"history_blob_refs", "footprint_writes", "footprint_reviews"} <= tables
        uv = await db.fetchone("PRAGMA user_version")
        assert uv is not None and uv[0] >= 12
    finally:
        await db.close()


async def test_sql_checks_reject_bad_op_and_state(db: Database) -> None:
    sql = (
        "INSERT INTO footprint_writes(project_id, lib_id, file_rel, op, state, created_at) "
        "VALUES (?, 'X:R', ?, ?, ?, '2026-11-18T00:00:00.000Z')"
    )
    await db.execute(sql, (P1, FILE, "create", "pending"))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        await db.execute(sql, (P1, FILE, "edit", "pending"))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        await db.execute(sql, (P1, FILE, "create", "done2"))


async def test_repo_validates_op_and_state(db: Database) -> None:
    repo = FootprintWriteRepo(db)
    with pytest.raises(ValueError):
        await repo.insert_pending(P1, "r1", "X:R", FILE, "edit", SHA_A)
    wid = await repo.insert_pending(P1, "r1", "X:R", FILE, "create", ABSENT)
    with pytest.raises(ValueError):
        await repo.finalise(wid, "done2", SHA_B)
    with pytest.raises(ValueError):
        await repo.finalise(wid, "pending", SHA_B)


async def test_insert_pending_and_finalise(db: Database) -> None:
    repo = FootprintWriteRepo(db)
    wid = await repo.insert_pending(P1, "r1", "X:R", FILE, "create", ABSENT, "g" * 64)
    assert wid >= 1
    [row] = await repo.pending(P1)
    assert row.id == wid and row.state == "pending" and row.op == "create"
    assert row.pre_sha256 == ABSENT and row.geometry_hash == "g" * 64
    assert row.run_id == "r1" and row.lib_id == "X:R" and row.file_rel == FILE
    assert row.created_at and row.finalised_at is None
    assert row.post_s2_sha256 is None and row.post_sha256 is None
    await repo.set_post_s2(wid, SHA_A)
    await repo.finalise(wid, "done", SHA_B)
    assert await repo.pending(P1) == []
    latest = await repo.latest_for_file(FILE)
    assert latest is not None
    assert latest.state == "done" and latest.post_s2_sha256 == SHA_A
    assert latest.post_sha256 == SHA_B and latest.finalised_at is not None


async def test_absent_round_trips(db: Database) -> None:
    repo = FootprintWriteRepo(db)
    wid = await repo.insert_pending(P1, None, "X:R", FILE, "delete", SHA_A)
    await repo.finalise(wid, "done", ABSENT)
    row = await repo.latest_for_file(FILE)
    assert row is not None and row.post_sha256 == ABSENT and row.run_id is None


async def test_pending_is_per_project_and_ordered(db: Database) -> None:
    repo = FootprintWriteRepo(db)
    a = await repo.insert_pending(P1, "r1", "X:A", "a.kicad_mod", "create", ABSENT)
    await repo.insert_pending(P2, "r2", "X:B", "b.kicad_mod", "create", ABSENT)
    c = await repo.insert_pending(P1, "r1", "X:C", "c.kicad_mod", "replace", SHA_A)
    assert [r.id for r in await repo.pending(P1)] == [a, c]


async def test_latest_for_file(db: Database) -> None:
    repo = FootprintWriteRepo(db)
    assert await repo.latest_for_file(FILE) is None
    await repo.insert_pending(P1, "r1", "X:R", FILE, "create", ABSENT)
    await repo.insert_pending(P1, "r1", "X:S", "other.kicad_mod", "create", ABSENT)
    last = await repo.insert_pending(P2, "r2", "X:R", FILE, "approve", SHA_A)
    row = await repo.latest_for_file(FILE)
    assert row is not None and row.id == last and row.project_id == P2


async def test_later_foreign(db: Database) -> None:
    repo = FootprintWriteRepo(db)
    own = await repo.insert_pending(P1, "r1", "X:R", FILE, "create", ABSENT)
    assert not await repo.later_foreign(FILE, P1, own)
    await repo.insert_pending(P1, "r1", "X:R", FILE, "approve", SHA_A)  # own, newer
    assert not await repo.later_foreign(FILE, P1, own)
    await repo.insert_pending(P2, "r2", "X:S", "other.kicad_mod", "create", ABSENT)
    assert not await repo.later_foreign(FILE, P1, own)  # foreign, other file
    foreign = await repo.insert_pending(P2, "r2", "X:R", FILE, "replace", SHA_A)
    assert await repo.later_foreign(FILE, P1, own)
    assert not await repo.later_foreign(FILE, P1, foreign)  # not newer than after_id
    assert not await repo.later_foreign(FILE, P2, foreign)
    assert await repo.later_foreign(FILE, P2, own)  # P1's newer approve is foreign to P2


async def test_foreign_after(db: Database) -> None:
    repo = FootprintWriteRepo(db)
    own = await repo.insert_pending(P1, "r1", "X:R", FILE, "create", ABSENT)
    assert await repo.foreign_after(P1, own) == set()
    await repo.insert_pending(P1, "r1", "X:R", FILE, "approve", SHA_A)  # own, newer
    assert await repo.foreign_after(P1, own) == set()
    first = await repo.insert_pending(P2, "r2", "X:S", "other.kicad_mod", "create", ABSENT)
    await repo.insert_pending(P2, "r2", "X:R", FILE, "replace", SHA_A)
    await repo.insert_pending(P2, "r2", "X:R", FILE, "approve", SHA_A)  # distinct
    assert await repo.foreign_after(P1, own) == {FILE, "other.kicad_mod"}
    assert await repo.foreign_after(P1, first) == {FILE}  # not newer than after_id: excluded
    assert await repo.foreign_after(P2, 0) == {FILE}  # P1's writes are foreign to P2


async def test_max_id_and_after_id(db: Database) -> None:
    repo = FootprintWriteRepo(db)
    assert await repo.max_id() == 0
    assert await repo.after_id(P1, 0) == []
    a = await repo.insert_pending(P1, "r1", "X:A", "a.kicad_mod", "create", ABSENT)
    b = await repo.insert_pending(P2, "r2", "X:B", "b.kicad_mod", "create", ABSENT)
    c = await repo.insert_pending(P1, "r1", "X:C", "c.kicad_mod", "row_add", ABSENT)
    await repo.finalise(c, "leftover", SHA_A)
    assert await repo.max_id() == c
    assert [r.id for r in await repo.after_id(P1, 0)] == [a, c]
    assert [r.id for r in await repo.after_id(P1, a)] == [c]
    assert [r.id for r in await repo.after_id(P2, a)] == [b]
    assert await repo.after_id(P1, c) == []
