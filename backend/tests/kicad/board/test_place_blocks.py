"""SP4 round 2 R2 Task 3: `pcb_floorplan` (read) and `pcb_place_blocks` (write) on
`sp4_synth` through the real tools: sync → outline 60 x 45 → rules → `pcb_define_blocks`
from `pcb_analyze` (the ESD part joins the USB connector's block) → floorplan / place."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from copperchat.agent.broadcaster import Broadcaster
from copperchat.board.model import board_hash, placement_hash
from copperchat.board.placer.floorplan import SPREAD_SPAN_MIN
from copperchat.design_docs.service import DesignDocs, policy_for
from copperchat.kicad_tools.board.read import board_model
from copperchat.tools.spec import ToolContext
from tests.kicad.board.conftest import (
    SETUP_OUTLINE,
    SETUP_RULES,
    SynthRig,
    SynthSetup,
    analyzed_blocks,
    setup_call,
)
from tests.support.edit import call_tool

pytestmark = [pytest.mark.kicad]

PLACEMENT_EDGES = (
    "| Ref | Edge/position | Reason |\n|---|---|---|\n| J1 | left | USB connector at the edge |\n"
)


async def _define(ctx: ToolContext) -> list[dict[str, Any]]:
    blocks = await analyzed_blocks(ctx)
    by_anchor = {b.get("anchor"): b for b in blocks}
    esd, usb = by_anchor.get("U2"), by_anchor.get("J1")
    if esd is not None and usb is not None:
        usb["refs"] = usb["refs"] + esd["refs"]
        blocks.remove(esd)
    await setup_call(ctx, "pcb_define_blocks", blocks=blocks, replace=True)
    return blocks


def _with_docs(ctx: ToolContext) -> ToolContext:
    assert ctx.editing is not None
    return dataclasses.replace(ctx, docs=DesignDocs(ctx.editing.db, ctx.settings, Broadcaster()))


async def _edges_row(ctx: ToolContext) -> None:
    assert ctx.docs is not None
    await ctx.docs.patch_section(
        ctx.project, "board.md", "Placement", PLACEMENT_EDGES, None, create=True,
        run_id=ctx.run_id, policy=policy_for("board"), summary="USB connector on the left edge",
    )  # fmt: skip


@pytest.mark.asyncio(loop_scope="module")  # synth_setup is module-scoped
async def test_floorplan_then_place_blocks(synth_setup: SynthSetup, setup_db: Any) -> None:
    rig, db = synth_setup, setup_db
    ctx = _with_docs(rig.turn(db, "run-fp-1"))
    blocks = await _define(ctx)
    await _edges_row(ctx)
    before = await board_model(ctx)

    # pcb_floorplan: regions for every block, nothing written
    ctx = _with_docs(rig.turn(db, "run-fp-2"))
    fp, err = await call_tool(ctx, "pcb_floorplan", keep_channels=1.5)
    assert not err, fp
    assert sorted(r["block"] for r in fp["regions"]) == sorted(b["name"] for b in blocks)
    usb = next(b["name"] for b in blocks if b.get("anchor") == "J1")
    assert fp["fixed"] == [usb]
    o = before.outline
    strip = next(r for r in fp["regions"] if r["block"] == usb)
    assert strip["x"] < o.x + 1.0  # the strip along the left edge
    assert 0 < fp["courtyard_fill"] < 1
    assert fp["png_url"].endswith(".png")
    after_fp = await board_model(ctx)
    assert board_hash(after_fp) == board_hash(before) == fp["board_hash"]

    # pcb_place_blocks: one change set, M1 pass, well spread
    ctx = _with_docs(rig.turn(db, "run-fp-3"))
    out = await setup_call(ctx, "pcb_place_blocks", keep_channels=1.5)
    assert out["logical_changes"] == 1
    assert out["placement_hash"]["changed"] is True
    assert out["unplaced_blocks"] == []
    assert out["spread"] >= SPREAD_SPAN_MIN and out["well_spread"] is True
    assert out["score"]["after"]["m1_violations"] == 0
    assert out["score"]["after"]["spread"] == out["spread"]
    assert [r["block"] for r in out["regions"]] == [r["block"] for r in fp["regions"]]
    row = await db.fetchone(
        "SELECT COUNT(*) FROM changes WHERE kind = 'board_place_blocks' AND run_id = 'run-fp-3'"
    )
    assert row is not None and row[0] == 1
    metrics = await setup_call(rig.turn(db, "run-fp-4"), "pcb_metrics", only=["M1", "M16"])
    m1 = next(m for m in metrics["metrics"] if m["id"] == "M1")
    assert m1["verdict"] == "pass", m1
    # rule (b) (R1: the placer's target is M16's placement-stage "alignable" count): the two
    # pairs left are blocked by an IC align_pairs never moves - C1 next to U1 (U1 / Y1 in
    # the way), R3 next to J1 (the ESD array U2 in the way)
    m16 = next(m for m in metrics["metrics"] if m["id"] == "M16")
    alignable = sorted(o["what"] for o in m16["offenders"] if o["what"].startswith("alignable"))
    assert [a.split(":")[0] for a in alignable] == [
        "alignable +3V3 C1.1-U1.5",
        "alignable /CC2 J1.B5-R3.1",
    ], alignable
    placed = await board_model(ctx)
    j1 = placed.part("J1")
    assert min(x for x, _ in j1.courtyard) < o.x + 2.0  # J1 on its board.md edge


async def test_place_blocks_undo_restores_staging(synth_rig: SynthRig) -> None:
    """History undo of the turn restores the staging positions; the same unplaced state
    then places exactly the same way again (one run id: `restore_turn` restores the
    snapshot `on_mutation` took right before the last call)."""
    await setup_call(synth_rig.ctx(), "pcb_sync_from_schematic")
    await setup_call(synth_rig.ctx(), "pcb_set_outline", **SETUP_OUTLINE)
    await setup_call(synth_rig.ctx(), "pcb_set_rules", **SETUP_RULES)
    await _define(synth_rig.ctx())
    staging = placement_hash(await board_model(synth_rig.ctx()))
    ctx = synth_rig.ctx()
    out = await setup_call(ctx, "pcb_place_blocks")
    assert out["placement_hash"]["before"] == staging
    assert out["placement_hash"]["after"] != staging
    await synth_rig.restore_turn(ctx)
    assert placement_hash(await board_model(synth_rig.ctx())) == staging
    again = await setup_call(synth_rig.ctx(), "pcb_place_blocks")
    assert again["placement_hash"]["after"] == out["placement_hash"]["after"]
