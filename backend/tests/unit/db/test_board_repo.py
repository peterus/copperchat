"""0013_board (B-C10, P3b Task 1): the board checkpoint and evidence tables and their
repositories (`copperchat.db.repos.board`)."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from copperchat.db import MIGRATIONS_DIR, Database, migrate
from copperchat.db.repos.board import BoardCheckpointRepo, BoardReviewRepo, board_file
from copperchat.projects import Project

PID = "a1b2c3d4e5f6"
BOARD = "main.kicad_pcb"
CID, OTHER = "c" * 32, "d" * 32
HA, HB = "a" * 64, "b" * 64
PA, PB = "1" * 64, "2" * 64


async def _raw(path: Path) -> Database:
    conn = await aiosqlite.connect(path, isolation_level=None)
    conn.row_factory = aiosqlite.Row
    return Database(conn)


def _upto(tmp: Path, last: int) -> Path:
    d = tmp / f"upto{last}"
    d.mkdir()
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(f.name[:4]) <= last:
            shutil.copy(f, d / f.name)
    return d


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    d = await Database.open(tmp_path / "board.db")
    try:
        yield d
    finally:
        await d.close()


async def _tables(db: Database) -> set[str]:
    rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")
    return {str(r[0]) for r in rows}


async def test_migration_0013_applies(tmp_path: Path) -> None:
    fresh = await Database.open(tmp_path / "fresh.db")
    try:
        names = await _tables(fresh)
        assert {"board_checkpoints", "board_reviews", "board_checkpoints_latest"} <= names
        assert {"board_reviews_placement", "board_reviews_board"} <= names
    finally:
        await fresh.close()
    old = await _raw(tmp_path / "old.db")
    try:
        await migrate(old, _upto(tmp_path, 12))
        assert "board_checkpoints" not in await _tables(old)
        await migrate(old)
        assert {"board_checkpoints", "board_reviews"} <= await _tables(old)
        row = await old.fetchone("SELECT MAX(version) FROM schema_migrations")
        assert row is not None and int(row[0]) >= 13
    finally:
        await old.close()


async def test_stage_check_constraint(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await BoardCheckpointRepo(db).approve(PID, BOARD, "x", HA, CID, "r1", "looks good")
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO board_checkpoints(project_id, board_file, stage, hash, conversation_id,"
            " run_id, user_quote, approved_at) VALUES (?, ?, 'routing', ?, ?, 'r', 'q', 't')",
            (PID, BOARD, HA, CID),
        )


async def test_kind_and_level_checks(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO board_reviews(project_id, board_file, placement_hash, board_hash, kind,"
            " created_at) VALUES (?, ?, ?, ?, 'photo', 't')",
            (PID, BOARD, PA, HA),
        )
    with pytest.raises(sqlite3.IntegrityError):
        await BoardReviewRepo(db).record_check(
            PID,
            BOARD,
            CID,
            "r1",
            level="medium",
            hard_failures=0,
            m1_pass=True,
            placement_hash=PA,
            board_hash=HA,
            stage="placement",
        )


async def test_latest_is_highest_id(db: Database) -> None:
    repo = BoardCheckpointRepo(db)
    assert await repo.latest(PID, BOARD, "placement") is None
    first = await repo.approve(PID, BOARD, "placement", PA, CID, "r1", "looks good")
    second = await repo.approve(PID, BOARD, "placement", PB, CID, "r2", "fine now")
    await repo.approve(PID, BOARD, "board", HA, CID, "r3", "ship it now")
    await repo.approve(PID, "other.kicad_pcb", "placement", PA, CID, "r4", "other one")
    latest = await repo.latest(PID, BOARD, "placement")
    assert latest is not None and latest.id == second > first
    assert latest.hash == PB and latest.user_quote == "fine now" and latest.run_id == "r2"
    assert latest.stage == "placement" and latest.conversation_id == CID
    assert latest.approved_at.endswith("Z")


async def test_evidence_filters_conversation_and_hash(db: Database) -> None:
    repo = BoardReviewRepo(db)
    r1 = await repo.record_render(
        PID, BOARD, CID, "r1", view="placement", placement_hash=PA, board_hash=HA
    )
    await repo.record_render(PID, BOARD, OTHER, "r9", view="placement", placement_hash=PA,
                             board_hash=HA)  # fmt: skip
    r3 = await repo.record_check(
        PID, BOARD, CID, "r1", level="quick", hard_failures=2, m1_pass=True,
        placement_hash=PA, board_hash=HB, stage="placement",
    )  # fmt: skip
    await repo.record_render(PID, BOARD, CID, "r1", view="top", placement_hash=PB, board_hash=HB)
    await repo.record_render(PID, "x.kicad_pcb", CID, "r1", view="top", placement_hash=PA,
                             board_hash=HA)  # fmt: skip
    by_placement = await repo.evidence(PID, BOARD, CID, placement_hash=PA)
    assert [r.id for r in by_placement] == [r1, r3]
    assert by_placement[0].kind == "render" and by_placement[0].view == "placement"
    check = by_placement[1]
    assert check.kind == "check" and check.check_level == "quick"
    assert check.hard_failures == 2 and check.m1_pass == 1 and check.stage == "placement"
    by_board = await repo.evidence(PID, BOARD, CID, board_hash=HB)
    assert [r.kind for r in by_board] == ["check", "render"]
    both = await repo.evidence(PID, BOARD, CID, placement_hash=PA, board_hash=HA)
    assert [r.id for r in both] == [r1]
    assert len(await repo.evidence(PID, BOARD, CID)) == 3


async def test_last_full_check_newest(db: Database) -> None:
    repo = BoardReviewRepo(db)
    assert await repo.last_full_check(PID, BOARD, HA) is None
    kw = {"placement_hash": PA, "board_hash": HA, "stage": "board"}
    await repo.record_check(
        PID, BOARD, CID, "r1", level="full", hard_failures=3, m1_pass=True, **kw
    )
    newest = await repo.record_check(
        PID, BOARD, OTHER, "r2", level="full", hard_failures=0, m1_pass=False, **kw
    )
    await repo.record_check(
        PID, BOARD, CID, "r3", level="quick", hard_failures=5, m1_pass=True, **kw
    )
    got = await repo.last_full_check(PID, BOARD, HA)
    assert got is not None and got.id == newest and got.hard_failures == 0 and got.m1_pass == 0
    assert await repo.last_full_check(PID, BOARD, HB) is None


def test_board_file_relative_posix(tmp_path: Path) -> None:
    proj = Project(
        id=PID, name="main", rel_path="main", dir=tmp_path, pro_file=tmp_path / "main.kicad_pro",
        sch_file=tmp_path / "main.kicad_sch", pcb_file=tmp_path / "hw" / "main.kicad_pcb",
    )  # fmt: skip
    assert board_file(proj) == "hw/main.kicad_pcb"
