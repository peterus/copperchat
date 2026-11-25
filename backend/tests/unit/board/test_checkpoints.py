"""Board checkpoint state and the checkpoint guards for later tools (P3b Task 3, B-C10,
spec §8.3): only the latest approval per stage counts; any hash change makes it stale."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from copperchat.board.model import BoardModel, board_hash, placement_hash
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.db.repos.board import BoardCheckpointRepo, BoardReviewRepo
from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.board.checkpoints import (
    checkpoint_state,
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
