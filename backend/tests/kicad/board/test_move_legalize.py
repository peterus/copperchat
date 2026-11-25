"""`pcb_move` and `pcb_legalize` on the real api-server (SP4 P4 Task 4), on `sp4_synth`
after sync, outline and rules (`synth_setup`) with every part placed legally in a grid by
one `pcb_move` (`placed`). The board persists across the module: tests run in file order
and every test that makes an overlap resolves it again."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from copperchat.board.model import BoardModel, board_hash, placement_hash
from copperchat.board.staging import part_box
from copperchat.db import Database
from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.board.checkpoints import (
    checkpoint_state,
    require_placement_approved,
)
from copperchat.kicad_tools.board.read import board_model
from copperchat.tools.spec import ToolContext
from tests.kicad.board.conftest import SynthSetup, open_setup_db, setup_call
from tests.kicad.board.test_approve import APPROVE_TEXT, approve_placement, grid_moves
from tests.support.edit import call_tool

pytestmark = [pytest.mark.kicad, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def placed(
    synth_setup: SynthSetup, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[SynthSetup]:
    """Every part but the mounting holes inside the outline, by one `pcb_move`."""
    tmp = tmp_path_factory.mktemp("move-placed")
    db = await open_setup_db(tmp / "placed.db", synth_setup.session.project.id)
    try:
        ctx = synth_setup.turn(db, "run-grid")
        moves = grid_moves(await board_model(ctx))
        items = [{"ref": r, "x": x, "y": y} for r, (x, y) in sorted(moves.items())]
        out = await setup_call(ctx, "pcb_move", items=items)
        assert out["logical_changes"] == -(-len(items) // 10)
        yield synth_setup
    finally:
        await db.close()


async def _move(ctx: ToolContext, **args: Any) -> dict[str, Any]:
    return await setup_call(ctx, "pcb_move", **args)


async def _refused(ctx: ToolContext, name: str, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx, name, **args)
    assert err, out
    error: dict[str, Any] = out["error"]
    return error


async def _hashes(ctx: ToolContext) -> tuple[str, str]:
    m = await board_model(ctx)
    return placement_hash(m), board_hash(m)


def _overlap(m: BoardModel, a: str, b: str) -> bool:
    pa, pb = part_box(m.part(a)), part_box(m.part(b))
    return pa[0] < pb[2] and pb[0] < pa[2] and pa[1] < pb[3] and pb[1] < pa[3]


def _overlaps(m: BoardModel) -> list[tuple[str, str]]:
    placed = sorted((p for p in m.parts if m.placed(p)), key=lambda p: p.ref)
    return [
        (a.ref, b.ref)
        for i, a in enumerate(placed)
        for b in placed[i + 1 :]
        if a.side == b.side and _overlap(m, a.ref, b.ref)
    ]


async def test_move_invalidates_placement_approval(placed: SynthSetup, setup_db: Database) -> None:
    await approve_placement(placed, setup_db, "run-m0", APPROVE_TEXT)
    ctx = placed.turn(setup_db, "run-m0-3")
    m = await board_model(ctx)
    await require_placement_approved(ctx, m)
    p = m.part("R1")
    await _move(ctx, items=[{"ref": "R1", "x": p.x + 0.5}])
    m = await board_model(ctx)
    state = await checkpoint_state(setup_db, ctx.project, m)
    assert state["placement"]["approved"] is True and state["placement"]["stale"] is True
    with pytest.raises(StructuredToolError) as err:
        await require_placement_approved(ctx, m)
    assert err.value.code == "placement_not_approved"
    await _move(ctx, items=[{"ref": "R1", "x": p.x}])  # back


async def test_move_exact(placed: SynthSetup, setup_db: Database) -> None:
    ctx = placed.turn(setup_db, "run-m1")
    p = (await board_model(ctx)).part("R2")
    x, y = round(p.x + 0.123, 3), round(p.y - 0.077, 3)
    out = await _move(ctx, items=[{"ref": "R2", "x": x, "y": y, "rot": 90}])
    q = (await board_model(ctx)).part("R2")
    assert (q.x, q.y) == pytest.approx((x, y), abs=1e-3)
    assert q.rot == pytest.approx(90, abs=0.01)
    assert out["moved"] == ["R2"] and out["moved_omitted"] == 0
    assert out["placement_hash"]["changed"] is True
    assert out["unrouted_nets"] == []
    assert set(out["score"]) == {"before", "after"}
    await _move(ctx, items=[{"ref": "R2", "x": p.x, "y": p.y, "rot": p.rot}])  # back


async def test_move_flip_side(placed: SynthSetup, setup_db: Database) -> None:
    """J2 (a THT pin header: no SMD pad) to the bottom side and back, with a new position
    and rotation in the same commit (probe: flip + update in one commit)."""
    ctx = placed.turn(setup_db, "run-m2")
    p = (await board_model(ctx)).part("J2")
    assert not any(q.smd for q in p.pads)
    out = await _move(ctx, items=[{"ref": "J2", "side": "bottom", "x": p.x + 1, "rot": 180}])
    q = (await board_model(ctx)).part("J2")
    assert q.side == "bottom"
    assert (q.x, q.y) == pytest.approx((p.x + 1, p.y), abs=1e-3)
    assert q.rot == pytest.approx(180, abs=0.01)
    assert out["moved"] == ["J2"]
    await _move(ctx, items=[{"ref": "J2", "side": "top", "x": p.x, "rot": p.rot}])
    r = (await board_model(ctx)).part("J2")
    assert (r.side, round(r.x, 3), round(r.rot, 2)) == (p.side, round(p.x, 3), round(p.rot, 2))
    # an SMD part to the bottom: refused by the side policy (no board.md: top only)
    error = await _refused(ctx, "pcb_move", items=[{"ref": "R3", "side": "bottom"}])
    assert error["code"] == "invalid_argument", error


async def test_move_with_legalize(placed: SynthSetup, setup_db: Database) -> None:
    ctx = placed.turn(setup_db, "run-m3")
    m = await board_model(ctx)
    r5, r6 = m.part("R5"), m.part("R6")
    target = {"ref": "R5", "x": r6.x + 0.2, "y": r6.y + 0.1}
    out = await _move(ctx, items=[target], legalize=True)
    m = await board_model(ctx)
    assert not _overlap(m, "R5", "R6")
    assert _overlaps(m) == []
    assert out["legalized"] is True and out["moved"] == ["R5"]
    await _move(ctx, items=[{"ref": "R5", "x": r5.x, "y": r5.y}])  # back


async def test_move_cost_ceil(placed: SynthSetup, setup_db: Database) -> None:
    ctx = placed.turn(setup_db, "run-m4")
    m = await board_model(ctx)
    refs = sorted(p.ref for p in m.parts if p.ref.startswith(("C", "R")))[:11]
    assert len(refs) == 11
    out = await _move(ctx, items=[{"ref": r, "y": m.part(r).y + 0.05} for r in refs])
    assert out["logical_changes"] == 2
    assert out["budget"] == {"used": 2, "limit": 20}
    assert len(out["moved"]) == 11
    await _move(ctx, items=[{"ref": r, "y": m.part(r).y} for r in refs])


async def test_legalize_locks_fixed(placed: SynthSetup, setup_db: Database) -> None:
    ctx = placed.turn(setup_db, "run-m5")
    m = await board_model(ctx)
    c4, c5 = m.part("C4"), m.part("C5")
    out = await _move(ctx, items=[{"ref": "C4", "locked": True},
                                  {"ref": "C5", "x": c4.x + 0.3, "y": c4.y}])  # fmt: skip
    assert out["locked_changed"] == ["C4"]
    assert _overlap(await board_model(ctx), "C4", "C5")
    out = await setup_call(ctx, "pcb_legalize", scope="all")
    m = await board_model(ctx)
    assert (m.part("C4").x, m.part("C4").y) == (c4.x, c4.y) and m.part("C4").locked
    assert "C5" in out["moved"] and "C4" not in out["moved"]
    assert _overlaps(m) == []
    # already legal: nothing written, cost 0
    again = await setup_call(placed.turn(setup_db, "run-m5b"), "pcb_legalize", scope="all")
    assert again["moved"] == [] and again["logical_changes"] == 0
    assert again["placement_hash"]["changed"] is False
    await _move(ctx, items=[{"ref": "C4", "locked": False}, {"ref": "C5", "x": c5.x, "y": c5.y}])


async def test_legalize_max_shift_does_not_fit(placed: SynthSetup, setup_db: Database) -> None:
    ctx = placed.turn(setup_db, "run-m6")
    m = await board_model(ctx)
    r3, r4 = m.part("R3"), m.part("R4")
    await _move(ctx, items=[{"ref": "R3", "x": r4.x, "y": r4.y}])
    before = await _hashes(ctx)
    error = await _refused(ctx, "pcb_legalize", scope="all", max_shift=0.05)
    assert error["code"] == "does_not_fit", json.dumps(error)
    assert {"needed_mm2", "available_mm2", "refs"} <= set(error["details"])
    assert await _hashes(ctx) == before
    # an unknown block
    error = await _refused(ctx, "pcb_legalize", scope="block", block="nope")
    assert error["code"] == "unknown_block", error
    await _move(ctx, items=[{"ref": "R3", "x": r3.x, "y": r3.y}])
    assert _overlaps(await board_model(ctx)) == []


async def test_move_unknown_ref(placed: SynthSetup, setup_db: Database) -> None:
    ctx = placed.turn(setup_db, "run-m7")
    error = await _refused(ctx, "pcb_move", items=[{"ref": "R77", "x": 120}])
    assert error["code"] == "not_found" and error["details"]["refs"] == ["R77"], error
    error = await _refused(ctx, "pcb_move", items=[{"ref": "R1"}])
    assert error["code"] == "invalid_argument", error
