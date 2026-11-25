"""Board checkpoint state and the checkpoint guards for later tools (P3b Task 3, B-C10,
spec §8.3): only the latest approval per stage counts; any hash change makes it stale."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from copperchat.board.model import BoardModel, board_hash, placement_hash
from copperchat.changes.repo import ChangeEntry, ChangeRepo
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.db.repos.board import BoardCheckpointRepo, BoardReviewRepo
from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.board.checkpoints import (
    board_written_since,
    checkpoint_state,
    record_unless_written,
    require_board_approved,
    require_placement_approved,
)
from copperchat.projects import Project
from copperchat.tools import ToolContext
from tests.unit.board.builders import board, with_part

PID = "a1b2c3d4e5f6"
BOARD = "main.kicad_pcb"
CID = "c" * 32


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    d = await Database.open(tmp_path / "cp.db")
    try:
        yield d
    finally:
        await d.close()


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project(
        id=PID, name="main", rel_path="main", dir=tmp_path, pro_file=tmp_path / "main.kicad_pro",
        sch_file=tmp_path / "main.kicad_sch", pcb_file=tmp_path / BOARD,
    )  # fmt: skip


def _ctx(project: Project, db: Database | None, settings: Settings) -> ToolContext:
    editing = None if db is None else SimpleNamespace(db=db)
    return ToolContext(
        project=project,
        session=cast(Any, None),
        settings=settings,
        editing=cast(Any, editing),
        conversation_id=CID,
        run_id="run-1",
    )


async def _approve(db: Database, stage: str, digest: str) -> None:
    await BoardCheckpointRepo(db).approve(PID, BOARD, stage, digest, CID, "run-1", "looks good")


async def _full_check(db: Database, m: BoardModel, hard: int) -> None:
    await BoardReviewRepo(db).record_check(
        PID, BOARD, CID, "run-1", level="full", hard_failures=hard, m1_pass=True,
        placement_hash=placement_hash(m), board_hash=board_hash(m), stage="board",
    )  # fmt: skip


async def test_state_missing(db: Database, project: Project) -> None:
    state = await checkpoint_state(db, project, board())
    for stage in ("placement", "board"):
        assert state[stage] == {
            "approved": False,
            "stale": False,
            "hash": None,
            "approved_at": None,
            "conversation_id": None,
        }


async def test_state_stale(db: Database, project: Project) -> None:
    m = board()
    await _approve(db, "placement", placement_hash(m))
    await _approve(db, "board", board_hash(m))
    state = await checkpoint_state(db, project, m)
    assert state["placement"]["approved"] and not state["placement"]["stale"]
    assert state["placement"]["hash"] == placement_hash(m)
    assert state["placement"]["conversation_id"] == CID and state["placement"]["approved_at"]
    assert state["board"]["approved"] and not state["board"]["stale"]
    moved = with_part(m, "R1", x=30.01)
    state = await checkpoint_state(db, project, moved)
    assert state["placement"]["stale"] and state["board"]["stale"]
    assert state["placement"]["hash"] == placement_hash(m)


async def test_only_latest_counts(
    db: Database, project: Project, make_settings: Callable[..., Settings]
) -> None:
    """Approve A, then B; the board back at A: the older approval is not revived."""
    a = board()
    b = with_part(a, "R1", x=31.0)
    await _approve(db, "placement", placement_hash(a))
    await _approve(db, "placement", placement_hash(b))
    state = await checkpoint_state(db, project, a)
    assert state["placement"]["approved"] and state["placement"]["stale"]
    ctx = _ctx(project, db, make_settings())
    with pytest.raises(StructuredToolError) as err:
        await require_placement_approved(ctx, a)
    assert err.value.code == "placement_not_approved"
    assert err.value.details == {"stale": True, "placement_hash": placement_hash(a)}
    await require_placement_approved(ctx, b)


async def test_require_placement_missing(
    db: Database, project: Project, make_settings: Callable[..., Settings]
) -> None:
    with pytest.raises(StructuredToolError) as err:
        await require_placement_approved(_ctx(project, db, make_settings()), board())
    assert err.value.details == {"stale": False, "placement_hash": placement_hash(board())}


async def test_require_board_check_failed(
    db: Database, project: Project, make_settings: Callable[..., Settings]
) -> None:
    m = board()
    ctx = _ctx(project, db, make_settings())

    async def reason() -> str:
        with pytest.raises(StructuredToolError) as err:
            await require_board_approved(ctx, m)
        assert err.value.code == "not_approved" and err.value.details is not None
        return str(err.value.details["reason"])

    assert await reason() == "missing"
    await _approve(db, "board", board_hash(m))
    assert await reason() == "check_failed"  # no full check at all
    await _full_check(db, m, hard=0)
    await _full_check(db, m, hard=1)  # the newest full check counts
    assert await reason() == "check_failed"
    await _full_check(db, m, hard=0)
    await require_board_approved(ctx, m)
    moved = with_part(m, "R2", rot=90.0)
    with pytest.raises(StructuredToolError) as err:
        await require_board_approved(ctx, moved)
    assert err.value.details == {"reason": "stale", "board_hash": board_hash(moved)}


async def test_require_fails_closed_without_db(
    db: Database, project: Project, make_settings: Callable[..., Settings]
) -> None:
    m = board()
    await _approve(db, "placement", placement_hash(m))
    await _approve(db, "board", board_hash(m))
    await _full_check(db, m, hard=0)
    ctx = _ctx(project, None, make_settings())
    with pytest.raises(StructuredToolError) as err:
        await require_placement_approved(ctx, m)
    assert err.value.code == "placement_not_approved"
    with pytest.raises(StructuredToolError) as err:
        await require_board_approved(ctx, m)
    assert err.value.code == "not_approved"
    assert err.value.details is not None and err.value.details["reason"] == "missing"


async def test_other_board_file_does_not_count(
    db: Database, project: Project, make_settings: Callable[..., Settings]
) -> None:
    m = board()
    await BoardCheckpointRepo(db).approve(
        PID, "other.kicad_pcb", "placement", placement_hash(m), CID, "r", "looks good"
    )
    with pytest.raises(StructuredToolError):
        await require_placement_approved(_ctx(project, db, make_settings()), m)


async def test_record_unless_written(db: Database) -> None:
    since = "2026-11-25T10:00:00.000Z"

    async def write(kind: str, at: str) -> None:
        await ChangeRepo(db).add_many(
            [ChangeEntry(PID, "r", None, 0, "t", "modify", kind, "", "", created_at=at)]
        )

    calls: list[int] = []

    async def record() -> int:
        calls.append(1)
        return 1

    await write("board_move", "2026-11-25T09:59:59.999Z")  # before: ignored
    await write("board_approve", since)  # an approval is no write
    await write("symbol", since)  # a schematic edit is no board write
    assert not await board_written_since(db, PID, since)
    assert await record_unless_written(db, PID, since, record) is True
    await write("board_set_rules", since)  # a tie counts (fail closed)
    assert await board_written_since(db, PID, since)
    assert await record_unless_written(db, PID, since, record) is False
    assert calls == [1]
    assert not await board_written_since(db, "f" * 12, since)


async def test_require_board_stale_after_schematic_edit(
    db: Database, project: Project, make_settings: Callable[..., Settings]
) -> None:
    m = board()
    await _approve(db, "board", board_hash(m))
    await _full_check(db, m, hard=0)
    ctx = _ctx(project, db, make_settings())
    await require_board_approved(ctx, m)
    await ChangeRepo(db).add_many(
        [ChangeEntry(PID, "r", None, 0, "sch_place_symbol", "add", "symbol", "u", "/")]
    )
    with pytest.raises(StructuredToolError) as err:
        await require_board_approved(ctx, m)
    assert err.value.details is not None and err.value.details["reason"] == "stale"
    await require_placement_approved_after(ctx, db, m)


async def require_placement_approved_after(ctx: ToolContext, db: Database, m: BoardModel) -> None:
    """A schematic edit never touches the placement checkpoint."""
    await _approve(db, "placement", placement_hash(m))
    await require_placement_approved(ctx, m)


async def test_board_written_since_any_kind(db: Database) -> None:
    since = "2026-11-25T10:00:00.000Z"
    await ChangeRepo(db).add_many(
        [ChangeEntry(PID, "r", None, 0, "t", "add", "symbol", "u", "/", created_at=since)]
    )
    assert not await board_written_since(db, PID, since)
    assert await board_written_since(db, PID, since, any_kind=True)
    await db.execute("DELETE FROM changes")
    await ChangeRepo(db).add_many(
        [ChangeEntry(PID, "r", None, 0, "t", "add", "board_approve", "u", "", created_at=since)]
    )
    assert not await board_written_since(db, PID, since, any_kind=True)


def test_check_dir_refuses_symlinks_and_sweeps(tmp_path: Path) -> None:
    import os
    import time as time_mod

    from copperchat.kicad_tools.board.check import STALE_CHECK_S, _check_dir

    project = Project(
        id=PID, name="main", rel_path="main", dir=tmp_path / "p",
        pro_file=tmp_path / "p/m.kicad_pro", sch_file=tmp_path / "p/m.kicad_sch",
        pcb_file=tmp_path / "p" / BOARD,
    )  # fmt: skip
    project.dir.mkdir()
    ctx: Any = SimpleNamespace(project=project)
    first = _check_dir(ctx)
    old = first.parent / "old-left-over"
    old.mkdir()
    past = time_mod.time() - STALE_CHECK_S - 10
    os.utime(old, (past, past))
    second = _check_dir(ctx)
    assert second.exists() and first.exists() and not old.exists()
    outside = tmp_path / "outside"
    outside.mkdir()
    board_dir = project.dir / ".kicad-agent" / "board"
    import shutil

    shutil.rmtree(board_dir)
    board_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(StructuredToolError) as err:
        _check_dir(ctx)
    assert err.value.code == "output_path_unsafe"
    assert list(outside.iterdir()) == []


async def test_new_passing_check_does_not_revive_board_approval(
    db: Database, project: Project, make_settings: Callable[..., Settings]
) -> None:
    """Approve at H, a schematic edit, then a new passing full check at the same H: the
    approval the user gave before the edit stays stale."""
    import asyncio

    m = board()
    await _full_check(db, m, hard=0)
    await _approve(db, "board", board_hash(m))
    await asyncio.sleep(0.01)
    await ChangeRepo(db).add_many(
        [ChangeEntry(PID, "r", None, 0, "sch_place_symbol", "add", "symbol", "u", "/")]
    )
    await asyncio.sleep(0.01)
    await _full_check(db, m, hard=0)
    with pytest.raises(StructuredToolError) as err:
        await require_board_approved(_ctx(project, db, make_settings()), m)
    assert err.value.details is not None and err.value.details["reason"] == "stale"


async def test_record_unless_written_any_kind(db: Database) -> None:
    since = "2026-11-25T10:00:00.000Z"
    await ChangeRepo(db).add_many(
        [ChangeEntry(PID, "r", None, 0, "t", "add", "symbol", "u", "/", created_at=since)]
    )

    async def record() -> int:
        return 1

    assert await record_unless_written(db, PID, since, record) is True
    assert await record_unless_written(db, PID, since, record, any_kind=True) is False
