"""`pcb_approve` and the checkpoint guards on the real api-server (SP4 P3b Task 3), on
`sp4_synth` after sync, outline and rules (`synth_setup`). Turn 1 = tool calls (render,
check); turn 2 starts with a message the user typed (`user_says`). Board moves go through
board change sets (`apply_board_change_set`, change-log kind `board_move`) — the board is
only ever changed through KiCad's API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from copperchat.board.model import BoardModel, placement_hash
from copperchat.board.staging import part_box
from copperchat.db import Database
from copperchat.db.repos.board import BoardReviewRepo, board_file
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.board.checkpoints import (
    checkpoint_state,
    require_board_approved,
    require_placement_approved,
)
from copperchat.kicad_tools.board.engine import (
    BoardChangeSet,
    BoardStage,
    BoardStageContext,
    apply_board_change_set,
)
from copperchat.kicad_tools.board.read import board_model
from copperchat.kicad_tools.edit.changeset import Logical
from copperchat.kicad_tools.edit.engine import save_after_edit
from copperchat.tools.spec import ToolContext
from tests.kicad.board.conftest import SynthSetup, open_setup_db, setup_call, user_says
from tests.support.conversations import TEST_CID
from tests.support.edit import call_tool

pytestmark = [pytest.mark.kicad, pytest.mark.asyncio(loop_scope="module")]

APPROVE_TEXT = "looks good, approve placement"
INSET = 6.0  # clear of the corner hole courtyards (inset 3 mm, r ≈ 2.45 mm)
GAP = 1.0


# -- helpers ------------------------------------------------------------------------------


async def _tool(ctx: ToolContext, name: str, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx, name, **args)
    if err:
        pytest.fail(json.dumps(out, indent=1))
    return out


async def _refused(ctx: ToolContext, name: str, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx, name, **args)
    assert err, out
    error: dict[str, Any] = out["error"]
    return error


def _is_hole(lib_id: str) -> bool:
    return lib_id.split(":")[-1].startswith("MountingHole")


async def move_parts(ctx: ToolContext, moves: dict[str, tuple[float, float]]) -> None:
    """One board change set (kind `board_move`, one commit) moving footprints by ref to
    absolute positions, then saved — as a board write tool does."""
    from kipy.geometry import Vector2

    from copperchat.kicad_tools.units import mm_to_nm

    def run(h: KicadHandle) -> None:
        board = h.board()
        changed = []
        for fp in board.get_footprints():
            target = moves.get(str(fp.reference_field.text.value))
            if target is not None:
                fp.position = Vector2.from_xy(mm_to_nm(target[0]), mm_to_nm(target[1]))
                changed.append(fp)
        assert len(changed) == len(moves)
        board.update_items(changed)

    async def stage(sc: BoardStageContext) -> None:
        await sc.call(run, timeout=60)

    cs = BoardChangeSet(
        tool="pcb_move",
        kind="board_move",
        board_stages=[BoardStage("move", stage)],
        logical=Logical(1),
    )
    if ctx.on_mutation is not None:
        await ctx.on_mutation()
    await apply_board_change_set(ctx, cs)
    await save_after_edit(ctx, {"pcb"})


async def nudge(ctx: ToolContext, ref: str, dx: float) -> None:
    p = (await board_model(ctx)).part(ref)
    await move_parts(ctx, {ref: (p.x + dx, p.y)})


def grid_moves(m: BoardModel) -> dict[str, tuple[float, float]]:
    """Every part but the mounting holes in columns of courtyard boxes with 1 mm gaps,
    inside the outline inset 6 mm on every side."""
    o = m.outline
    x0, y0, y_end = o.x + INSET, o.y + INSET, o.y + o.h - INSET
    moves: dict[str, tuple[float, float]] = {}
    cx, cy, col_w = x0, y0, 0.0
    for p in sorted(m.parts, key=lambda p: p.ref):
        if _is_hole(p.lib_id):
            continue
        bx0, by0, bx1, by1 = part_box(p)
        w, h = bx1 - bx0, by1 - by0
        if cy + h > y_end:
            cx, cy, col_w = cx + col_w + GAP, y0, 0.0
        moves[p.ref] = (round(p.x + cx - bx0, 3), round(p.y + cy - by0, 3))
        cy += h + GAP
        col_w = max(col_w, w)
    assert cx + col_w <= o.x + o.w - INSET, "the grid does not fit the outline"
    return moves


async def show_placement(ctx: ToolContext) -> str:
    """Turn 1: placement render + quick check; returns the placement hash shown."""
    render = await _tool(ctx, "pcb_render", view="placement")
    check = await _tool(ctx, "pcb_check", level="quick")
    assert render["placement_hash"] == check["placement_hash"]
    m1 = next(m for m in check["metrics"] if m["id"] == "M1")
    assert m1["verdict"] == "pass", m1
    digest: str = check["placement_hash"]
    return digest


async def approve_placement(setup: SynthSetup, db: Database, run: str, text: str) -> str:
    """Show the placement in turn `run`-1, approve it in turn `run`-2; returns the hash."""
    digest = await show_placement(setup.turn(db, f"{run}-1"))
    await user_says(db, setup.session.project.id, f"{run}-2", text)
    out = await _tool(
        setup.turn(db, f"{run}-2"), "pcb_approve", stage="placement", hash=digest, user_quote=text
    )
    assert out["hash"] == digest
    return digest


# -- fixtures -----------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def placed(
    synth_setup: SynthSetup, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[SynthSetup]:
    """`synth_setup` with every part inside the outline (one board change set)."""
    tmp = tmp_path_factory.mktemp("placed")
    db = await open_setup_db(tmp / "placed.db", synth_setup.session.project.id)
    try:
        ctx = synth_setup.turn(db, "run-place")
        await move_parts(ctx, grid_moves(await board_model(ctx)))
        yield synth_setup
    finally:
        await db.close()


# -- tests --------------------------------------------------------------------------------


async def test_placement_approval_flow(placed: SynthSetup, setup_db: Database) -> None:
    ctx = placed.turn(setup_db, "run-a1")
    metrics = await _tool(ctx, "pcb_metrics", only=["M1"])
    m1 = metrics["metrics"][0]
    assert m1["id"] == "M1" and m1["verdict"] == "pass" and m1["value"] == 0, m1
    digest = await show_placement(ctx)
    rows = await BoardReviewRepo(setup_db).evidence(
        ctx.project.id, board_file(ctx.project), TEST_CID, placement_hash=digest
    )
    assert [(r.kind, r.view, r.check_level, r.m1_pass) for r in rows] == [
        ("render", "placement", None, None),
        ("check", None, "quick", 1),
    ]
    with pytest.raises(StructuredToolError) as err:
        await require_placement_approved(ctx, await board_model(ctx))
    assert err.value.code == "placement_not_approved"
    # the same turn: refused (guard 5)
    same = await _refused(
        ctx, "pcb_approve", stage="placement", hash=digest, user_quote=APPROVE_TEXT
    )
    assert same["code"] == "approval_not_allowed" and same["details"]["guard"] == 5
    await user_says(setup_db, ctx.project.id, "run-a2", APPROVE_TEXT)
    turn2 = placed.turn(setup_db, "run-a2")
    out = await _tool(turn2, "pcb_approve", stage="placement", hash=digest, user_quote=APPROVE_TEXT)
    assert out["stage"] == "placement" and out["hash"] == digest and out["approved_at"]
    await require_placement_approved(turn2, await board_model(turn2))


async def test_aba_kicad_refused(placed: SynthSetup, setup_db: Database) -> None:
    digest = await show_placement(placed.turn(setup_db, "run-b1"))
    pid = placed.session.project.id
    await user_says(setup_db, pid, "run-b2", APPROVE_TEXT)
    turn2 = placed.turn(setup_db, "run-b2")
    await nudge(turn2, "R1", 1.0)
    await nudge(turn2, "R1", -1.0)
    assert placement_hash(await board_model(turn2)) == digest  # A again
    error = await _refused(
        turn2, "pcb_approve", stage="placement", hash=digest, user_quote=APPROVE_TEXT
    )
    assert error["code"] == "approval_not_allowed" and error["details"]["guard"] == 4, error


async def test_older_approval_not_revived(placed: SynthSetup, setup_db: Database) -> None:
    a = await approve_placement(placed, setup_db, "run-c", APPROVE_TEXT)
    ctx = placed.turn(setup_db, "run-c3")
    await nudge(ctx, "R2", 1.0)
    b = await approve_placement(placed, setup_db, "run-d", "fine now, approve placement")
    assert b != a
    ctx = placed.turn(setup_db, "run-d3")
    await nudge(ctx, "R2", -1.0)
    m = await board_model(ctx)
    assert placement_hash(m) == a
    with pytest.raises(StructuredToolError) as err:
        await require_placement_approved(ctx, m)
    assert err.value.code == "placement_not_approved"
    assert err.value.details == {"stale": True, "placement_hash": a}
    state = await checkpoint_state(setup_db, ctx.project, m)
    assert state["placement"]["stale"] is True and state["placement"]["hash"] == b


async def test_placement_move_invalidates(placed: SynthSetup, setup_db: Database) -> None:
    await approve_placement(placed, setup_db, "run-e", APPROVE_TEXT)
    ctx = placed.turn(setup_db, "run-e3")
    await require_placement_approved(ctx, await board_model(ctx))
    await nudge(ctx, "C1", 0.01)
    with pytest.raises(StructuredToolError) as err:
        await require_placement_approved(ctx, await board_model(ctx))
    assert err.value.code == "placement_not_approved" and err.value.details is not None
    assert err.value.details["stale"] is True


async def test_approve_budget_cost_1(placed: SynthSetup, setup_db: Database) -> None:
    digest = await show_placement(placed.turn(setup_db, "run-f1"))
    await user_says(setup_db, placed.session.project.id, "run-f2", APPROVE_TEXT)
    out = await _tool(
        placed.turn(setup_db, "run-f2"),
        "pcb_approve",
        stage="placement",
        hash=digest,
        user_quote=APPROVE_TEXT,
    )
    assert out["logical_changes"] == 1
    assert out["budget"] == {"used": 1, "limit": 20}
    row = await setup_db.fetchone("SELECT COUNT(*) FROM changes WHERE kind = 'board_approve'")
    assert row is not None and row[0] == 1


async def test_dru_change_invalidates_board(placed: SynthSetup, setup_db: Database) -> None:
    """A full check without hard failures is not reachable on an unrouted board: the board
    evidence of the current board_hash is recorded through the repository."""
    ctx = placed.turn(setup_db, "run-g1")
    status = await _tool(ctx, "pcb_status")
    rel, pid = board_file(ctx.project), ctx.project.id
    repo = BoardReviewRepo(setup_db)
    hashes = {"placement_hash": status["placement_hash"], "board_hash": status["board_hash"]}
    await repo.record_render(pid, rel, TEST_CID, "run-g1", view="top", **hashes)
    await repo.record_check(pid, rel, TEST_CID, "run-g1", level="full", hard_failures=0,
                            m1_pass=True, stage="board", **hashes)  # fmt: skip
    await repo.record_render(pid, rel, TEST_CID, "run-g1", view="bottom", **hashes)
    text = "board is fine, approve the board"
    await user_says(setup_db, pid, "run-g2", text)
    turn2 = placed.turn(setup_db, "run-g2")
    await _tool(turn2, "pcb_approve", stage="board", hash=status["board_hash"], user_quote=text)
    await require_board_approved(turn2, await board_model(turn2))
    rule = {
        "name": "gnd_clear",
        "condition": "A.NetName == 'GND'",
        "constraint": "clearance",
        "min": 0.25,
        "reason": "test rule after the approval",
    }
    turn3 = placed.turn(setup_db, "run-g3")
    await setup_call(turn3, "pcb_set_rules", custom_rules=[rule])
    m = await board_model(turn3)
    with pytest.raises(StructuredToolError) as err:
        await require_board_approved(turn3, m)
    assert err.value.code == "not_approved" and err.value.details is not None
    assert err.value.details["reason"] == "stale"
    assert placement_hash(m) == status["placement_hash"]  # rules are no placement


async def test_approval_survives_new_session(placed: SynthSetup, setup_db: Database) -> None:
    digest = await approve_placement(placed, setup_db, "run-h", APPROVE_TEXT)
    project = placed.session.project
    await placed.sessions.reopen(project)
    ctx = placed.turn(setup_db, "run-h3")
    m = await board_model(ctx)
    assert placement_hash(m) == digest
    await require_placement_approved(ctx, m)
    state = await checkpoint_state(setup_db, ctx.project, m)
    assert state["placement"] == {
        "approved": True,
        "stale": False,
        "hash": digest,
        "approved_at": state["placement"]["approved_at"],
        "conversation_id": TEST_CID,
    }
