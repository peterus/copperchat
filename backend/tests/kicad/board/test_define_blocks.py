"""`pcb_define_blocks` on the real api-server (SP4 P4 Task 3), on `sp4_synth` after sync,
outline and rules (`synth_setup`). Blocks are KiCad groups `blk:<name>`; every test
starts from its own definition (`replace: true`) because the board persists across the
module."""

from __future__ import annotations

import json
from typing import Any

import pytest

from copperchat.board.model import BoardModel, board_hash, placement_hash
from copperchat.db import Database
from copperchat.kicad_tools.board import place
from copperchat.kicad_tools.board.read import board_model
from copperchat.tools.spec import ToolContext
from tests.kicad.board.conftest import SynthSetup, analyzed_blocks, setup_call
from tests.support.edit import call_tool

pytestmark = [pytest.mark.kicad, pytest.mark.asyncio(loop_scope="module")]


def _blocks(m: BoardModel) -> dict[str, set[str]]:
    return {n[4:]: set(refs) for n, refs in m.groups if n.startswith("blk:")}


async def _define(ctx: ToolContext, **args: Any) -> dict[str, Any]:
    return await setup_call(ctx, "pcb_define_blocks", **args)


async def _refused(ctx: ToolContext, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx, "pcb_define_blocks", **args)
    assert err, out
    error: dict[str, Any] = out["error"]
    return error


async def _hashes(ctx: ToolContext) -> tuple[str, str]:
    m = await board_model(ctx)
    return placement_hash(m), board_hash(m)


async def test_define_from_analyze(synth_setup: SynthSetup, setup_db: Database) -> None:
    ctx = synth_setup.turn(setup_db, "run-d1")
    blocks = await analyzed_blocks(ctx)
    assert len(blocks) >= 3
    out = await _define(ctx, blocks=blocks, replace=True)
    m = await board_model(ctx)
    assert _blocks(m) == {b["name"]: set(b["refs"]) for b in blocks}
    for b in blocks:
        assert all(m.part(r).group == f"blk:{b['name']}" for r in b["refs"])
    got = {b["name"]: b["count"] for b in out["blocks"]}
    assert got == {b["name"]: len(b["refs"]) for b in blocks}
    # only mounting holes are outside every block on sp4_synth
    assert all(r.startswith("H") for r in out["not_in_block"]), out["not_in_block"]
    assert out["next"] == "pcb_place_block"


async def test_replace_false_keeps_others(synth_setup: SynthSetup, setup_db: Database) -> None:
    ctx = synth_setup.turn(setup_db, "run-d2")
    await _define(ctx, blocks=[{"name": "a", "refs": ["R1", "R2"]},
                               {"name": "b", "refs": ["R3"]}], replace=True)  # fmt: skip
    out = await _define(ctx, blocks=[{"name": "c", "refs": ["R4"], "anchor": "R4"}])
    assert _blocks(await board_model(ctx)) == {"a": {"R1", "R2"}, "b": {"R3"}, "c": {"R4"}}
    assert out["defined"] == [{"name": "c", "count": 1, "anchor": "R4"}]
    assert out["removed"] == []
    # a same-named block is replaced, the others stay
    await _define(ctx, blocks=[{"name": "a", "refs": ["R5"]}])
    assert _blocks(await board_model(ctx)) == {"a": {"R5"}, "b": {"R3"}, "c": {"R4"}}


async def test_replace_true(synth_setup: SynthSetup, setup_db: Database) -> None:
    ctx = synth_setup.turn(setup_db, "run-d3")
    await _define(ctx, blocks=[{"name": "a", "refs": ["R1"]}, {"name": "b", "refs": ["R2"]}])
    out = await _define(ctx, blocks=[{"name": "only", "refs": ["R1", "C1"]}], replace=True)
    assert _blocks(await board_model(ctx)) == {"only": {"R1", "C1"}}
    assert "a" in out["removed"] and "b" in out["removed"]


async def test_ref_moves_between_blocks(synth_setup: SynthSetup, setup_db: Database) -> None:
    ctx = synth_setup.turn(setup_db, "run-d4")
    await _define(ctx, blocks=[{"name": "a", "refs": ["R1", "R2"]},
                               {"name": "b", "refs": ["R3"]}], replace=True)  # fmt: skip
    await _define(ctx, blocks=[{"name": "b", "refs": ["R3", "R2"]}])
    m = await board_model(ctx)
    assert _blocks(m) == {"a": {"R1"}, "b": {"R2", "R3"}}
    assert m.part("R2").group == "blk:b"
    # taking a block's last member removes the block
    out = await _define(ctx, blocks=[{"name": "b", "refs": ["R3", "R2", "R1"]}])
    assert _blocks(await board_model(ctx)) == {"b": {"R1", "R2", "R3"}}
    assert out["removed"] == ["a"]


async def test_unknown_ref_closest(synth_setup: SynthSetup, setup_db: Database) -> None:
    ctx = synth_setup.turn(setup_db, "run-d5")
    before = await _hashes(ctx)
    error = await _refused(ctx, blocks=[{"name": "x", "refs": ["R1", "U11"]}])
    assert error["code"] == "not_found", error
    assert error["details"]["refs"] == ["U11"]
    assert "U1" in error["details"]["closest"]["U11"]
    assert await _hashes(ctx) == before


async def test_hole_refused(synth_setup: SynthSetup, setup_db: Database) -> None:
    ctx = synth_setup.turn(setup_db, "run-d6")
    before = await _hashes(ctx)
    error = await _refused(ctx, blocks=[{"name": "x", "refs": ["R1", "H1"]}])
    assert error["code"] == "invalid_argument" and "H1" in error["why"], error
    error = await _refused(ctx, blocks=[{"name": "x", "refs": ["R1"]},
                                        {"name": "y", "refs": ["R1"]}])  # fmt: skip
    assert error["code"] == "invalid_argument", error
    error = await _refused(ctx, blocks=[{"name": "x", "refs": ["R1"], "anchor": "U1"}])
    assert error["code"] == "invalid_argument", error
    assert await _hashes(ctx) == before


async def test_fail_after_1(
    synth_setup: SynthSetup, setup_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after stage 1 (updates + creations; the deletion stage comes last) leaves
    the groups as they were: compensation removes the created group, the stage undo
    restores the members of every group the call touched."""
    ctx = synth_setup.turn(setup_db, "run-d7")
    await _define(ctx, blocks=[{"name": "a", "refs": ["R1", "R2"]},
                               {"name": "b", "refs": ["R3"]}], replace=True)  # fmt: skip
    before = await _hashes(ctx)
    real = place.apply_board_change_set

    async def failing(ctx: ToolContext, cs: Any) -> Any:
        cs.fail_after = 1
        return await real(ctx, cs)

    monkeypatch.setattr(place, "apply_board_change_set", failing)
    # a reused block, a member taken from b (b emptied → deleted last), a new block
    out, err = await call_tool(
        ctx,
        "pcb_define_blocks",
        blocks=[{"name": "a", "refs": ["R1", "R3"]}, {"name": "new", "refs": ["R2"]}],
    )
    assert err, out
    print("fail_after=1:", json.dumps(out["error"])[:400])
    assert out["error"].get("code") != "document_inconsistent", out
    assert await _hashes(ctx) == before
    assert _blocks(await board_model(ctx)) == {"a": {"R1", "R2"}, "b": {"R3"}}


async def test_budget_cost_1(synth_setup: SynthSetup, setup_db: Database) -> None:
    ctx = synth_setup.turn(setup_db, "run-d8")
    out = await _define(ctx, blocks=[{"name": "a", "refs": ["R1"]}], replace=True)
    assert out["logical_changes"] == 1
    assert out["budget"] == {"used": 1, "limit": 20}
    row = await setup_db.fetchone(
        "SELECT COUNT(*) FROM changes WHERE kind = 'board_define_blocks' AND run_id = 'run-d8'"
    )
    assert row is not None and row[0] == 1
