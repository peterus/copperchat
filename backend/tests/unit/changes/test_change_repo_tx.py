"""`ChangeRepo.add_many_in_transaction` (P3b): inserts inside the caller's open
transaction only; `add_many` keeps its own transaction."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from copperchat.changes.repo import ChangeEntry, ChangeRepo
from copperchat.db import Database

PID = "a1b2c3d4e5f6"


def _entry(uuid: str) -> ChangeEntry:
    return ChangeEntry(PID, "run-1", None, 0, "pcb_approve", "add", "board_approve", uuid, "")


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    d = await Database.open(tmp_path / "c.db")
    try:
        yield d
    finally:
        await d.close()


async def _count(db: Database) -> int:
    row = await db.fetchone("SELECT COUNT(*) FROM changes")
    assert row is not None
    return int(row[0])


async def test_add_many_in_transaction_requires_open_tx(db: Database) -> None:
    repo = ChangeRepo(db)
    with pytest.raises(RuntimeError):
        await repo.add_many_in_transaction([_entry("a")])
    assert await _count(db) == 0
    assert await repo.add_many([_entry("b"), _entry("c")]) == 2
    assert await repo.add_many([]) == 0
    assert await _count(db) == 2


async def test_in_transaction_commits_and_rolls_back(db: Database) -> None:
    repo = ChangeRepo(db)
    async with db.transaction():
        assert await repo.add_many_in_transaction([_entry("d")]) == 1
    assert await _count(db) == 1
    with pytest.raises(ValueError):
        async with db.transaction():
            await repo.add_many_in_transaction([_entry("e")])
            raise ValueError("roll back")
    assert await _count(db) == 1
    with pytest.raises(RuntimeError, match="nested"):
        async with db.transaction():
            await repo.add_many([_entry("f")])  # its own transaction: nested
    assert await _count(db) == 1
