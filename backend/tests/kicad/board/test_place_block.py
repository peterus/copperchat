"""`pcb_place_block` on the real api-server (SP4 P4 Task 4), on `sp4_synth` after sync,
outline (60 x 45 mm at (100, 100), corner holes) and rules (`synth_setup`), with the
blocks of `pcb_analyze` defined (`placed_blocks`). The board persists across the module:
tests run in file order."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from copperchat.board.model import BoardModel, board_hash, placement_hash
from copperchat.board.staging import part_box
from copperchat.db import Database
from copperchat.kicad_tools.board import api, place
from copperchat.kicad_tools.board.read import board_model
from copperchat.tools.spec import ToolContext
from tests.kicad.board.conftest import (
    SynthSetup,
    analyzed_blocks,
    open_setup_db,
    setup_call,
)
from tests.support.edit import call_tool

pytestmark = [pytest.mark.kicad, pytest.mark.asyncio(loop_scope="module")]

U1_REGION = {"x": 122.0, "y": 104.0, "w": 34.0, "h": 37.0}  # right part of the outline


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def blocks(
    synth_setup: SynthSetup, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[dict[str, list[str]]]:
    tmp = tmp_path_factory.mktemp("place-blocks")
    db = await open_setup_db(tmp / "blocks.db", synth_setup.session.project.id)
    try:
        ctx = synth_setup.turn(db, "run-blocks")
        found = await analyzed_blocks(ctx)
        await setup_call(ctx, "pcb_define_blocks", blocks=found, replace=True)
        yield {b["name"]: b["refs"] for b in found}
    finally:
        await db.close()


def _block_of(blocks: dict[str, list[str]], ref: str) -> str:
    return next(n for n, refs in blocks.items() if ref in refs)


async def _place(ctx: ToolContext, **args: Any) -> tuple[dict[str, Any], bool]:
    if ctx.on_mutation is not None:
        await ctx.on_mutation()
    return await call_tool(ctx, "pcb_place_block", **args)


async def _hashes(ctx: ToolContext) -> tuple[str, str]:
    m = await board_model(ctx)
    return placement_hash(m), board_hash(m)


def _inside(m: BoardModel, ref: str, r: dict[str, float], tol: float = 1e-3) -> bool:
    x0, y0, x1, y1 = part_box(m.part(ref))
    return (
        x0 >= r["x"] - tol
        and y0 >= r["y"] - tol
        and x1 <= r["x"] + r["w"] + tol
        and y1 <= r["y"] + r["h"] + tol
    )


async def test_place_block_one_commit(
    blocks: dict[str, list[str]], synth_setup: SynthSetup, setup_db: Database
) -> None:
    ctx = synth_setup.turn(setup_db, "run-p1")
    name = _block_of(blocks, "U1")
    out, err = await _place(ctx, block=name, region=U1_REGION)
    assert not err, json.dumps(out, indent=1)
    m = await board_model(ctx)
    refs = blocks[name]
    assert all(_inside(m, r, U1_REGION) for r in refs), [
        (r, part_box(m.part(r))) for r in refs if not _inside(m, r, U1_REGION)
    ]
    assert all(m.placed(m.part(r)) for r in refs)
    assert sorted(out["moved"]) == sorted(refs)
    assert out["placement_hash"]["changed"] is True
    assert out["placement_hash"]["after"] == placement_hash(m)
    assert out["score"]["before"] is not None and out["score"]["after"] is not None
    assert out["score"]["after"]["m1_violations"] <= out["score"]["before"]["m1_violations"]
    assert out["next"] == "pcb_place_block" and name not in out["unplaced_blocks"]
    assert out["logical_changes"] == 1
    drc = await ctx.session.call(lambda h: api.run_drc_task(h), timeout=api.drc_call_timeout(60))
    overlaps = [mk for mk in drc.markers if mk.type == "overlapping_footprints"]
    assert overlaps == [], overlaps
    rows = await setup_db.fetchall(
        "SELECT kind FROM changes WHERE run_id = 'run-p1' AND project_id = ?", (ctx.project.id,)
    )
    assert [r[0] for r in rows] == ["board_place_block"]
    print("place_block score:", out["score"]["after"])


async def test_edge_form(
    blocks: dict[str, list[str]], synth_setup: SynthSetup, setup_db: Database
) -> None:
    ctx = synth_setup.turn(setup_db, "run-p2")
    name = _block_of(blocks, "J1")
    out, err = await _place(ctx, block=name, region={"edge": "left"})
    assert not err, json.dumps(out, indent=1)
    m = await board_model(ctx)
    x0 = part_box(m.part("J1"))[0]
    print("J1 courtyard distance from the left edge:", x0 - m.outline.x, out["region"])
    assert x0 - m.outline.x <= 0.5
    assert out["region"]["x"] == m.outline.x and out["edge"] == "left"
    assert all(m.placed(m.part(r)) for r in blocks[name])


async def test_does_not_fit_writes_nothing(
    blocks: dict[str, list[str]], synth_setup: SynthSetup, setup_db: Database
) -> None:
    ctx = synth_setup.turn(setup_db, "run-p3")
    before = await _hashes(ctx)
    name = _block_of(blocks, "U3")
    out, err = await _place(ctx, block=name, region={"x": 110, "y": 120, "w": 3, "h": 3})
    assert err, out
    e = out["error"]
    assert e["code"] == "does_not_fit", e
    assert e["details"]["needed_mm2"] > e["details"]["available_mm2"]
    assert sorted(e["details"]["refs"]) == sorted(blocks[name])
    assert await _hashes(ctx) == before


async def test_bottom_refused_by_board_md(
    blocks: dict[str, list[str]], synth_setup: SynthSetup, setup_db: Database
) -> None:
    """No board.md (or no 'sides: top+bottom') → SMD parts stay on top."""
    ctx = synth_setup.turn(setup_db, "run-p4")
    before = await _hashes(ctx)
    out, err = await _place(
        ctx, block=_block_of(blocks, "U3"), region={"edge": "bottom"}, side="bottom"
    )
    assert err and out["error"]["code"] == "invalid_argument", out
    assert await _hashes(ctx) == before


async def test_unknown_block_closest(
    blocks: dict[str, list[str]], synth_setup: SynthSetup, setup_db: Database
) -> None:
    ctx = synth_setup.turn(setup_db, "run-p5")
    name = _block_of(blocks, "U1")
    out, err = await _place(ctx, block=name[:-1] + "x", region={"edge": "top"})
    assert err and out["error"]["code"] == "unknown_block", out
    assert name in out["error"]["details"]["closest"]


async def test_place_block_fail_after_1(
    blocks: dict[str, list[str]],
    synth_setup: SynthSetup,
    setup_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = synth_setup.turn(setup_db, "run-p6")
    before = await _hashes(ctx)
    real = place.apply_board_change_set

    async def failing(ctx: ToolContext, cs: Any) -> Any:
        cs.fail_after = 1
        return await real(ctx, cs)

    monkeypatch.setattr(place, "apply_board_change_set", failing)
    out, err = await _place(
        ctx, block=_block_of(blocks, "U3"), region={"x": 104, "y": 118, "w": 20, "h": 24}
    )
    assert err, out
    assert out["error"].get("code") != "document_inconsistent", out
    assert await _hashes(ctx) == before


async def test_place_power_block(
    blocks: dict[str, list[str]], synth_setup: SynthSetup, setup_db: Database
) -> None:
    """The power block into the free lower left part: no overlap with the placed blocks."""
    ctx = synth_setup.turn(setup_db, "run-p7")
    name = _block_of(blocks, "U3")
    region = {"x": 104, "y": 116, "w": 20, "h": 26}
    out, err = await _place(ctx, block=name, region=region)
    assert not err, json.dumps(out, indent=1)
    m = await board_model(ctx)
    assert all(_inside(m, r, region) for r in blocks[name])
    drc = await ctx.session.call(lambda h: api.run_drc_task(h), timeout=api.drc_call_timeout(60))
    overlaps = [mk for mk in drc.markers if mk.type == "overlapping_footprints"]
    assert overlaps == [], overlaps
    print("after power block:", out["score"]["after"], out["unplaced_blocks"])
