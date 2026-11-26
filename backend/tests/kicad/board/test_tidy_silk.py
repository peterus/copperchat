"""`pcb_tidy_silk` on the real api-server (SP4 P7 Task 5): the references of the routed,
zoned, fanned-out and stitched `sp4_synth` (`copper_rig`) placed readable (1.0 mm / 0.15
mm, on the part's side, no DRC silk marker), the board_hash change, the budget and the
`undo` of every stage.

With `P7_RENDER_DIR` set (local runs) the top and In1.Cu renders after the tidy are kept
there (handed to the owner)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from kipy.geometry import Vector2

from copperchat.board.model import BoardModel, board_hash
from copperchat.kicad_tools.board import api, silk
from copperchat.kicad_tools.board.read import board_model
from copperchat.kicad_tools.units import mm_to_nm
from copperchat.projects import agent_dir
from tests.kicad.board.conftest import PlacedSynth, ensure_stitch, setup_call

pytestmark = [
    pytest.mark.kicad,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.timeout(900),  # the first test of a shard builds the session rig
]


async def _model(rig: PlacedSynth, run: str) -> BoardModel:
    return await board_model(rig.turn(run))


def _ref(m: BoardModel, ref: str) -> Any:
    return next(t for t in m.part(ref).texts if t.kind == "reference")


async def _perturb(rig: PlacedSynth, ref: str) -> None:
    """Put `ref`'s reference onto its part's centre (over its pads): a test-only API write."""
    ctx = rig.turn(f"run-perturb-{ref}")
    m = await board_model(ctx)
    p = m.part(ref)

    def move(h: Any) -> None:
        board = h.board()
        fp = next(f for f in board.get_footprints() if str(f.id.value) == p.kiid)
        fp.reference_field.text.position = Vector2.from_xy(mm_to_nm(p.x), mm_to_nm(p.y))
        board.update_items([fp])

    async with ctx.session.commit("test: perturb reference", doc="pcb"):
        await ctx.session.call(move, timeout=120)


async def _drc(rig: PlacedSynth) -> api.DrcResult:
    result: api.DrcResult = await rig.session.call(
        lambda h: api.run_drc_task(h, timeout_s=60), timeout=api.drc_call_timeout(60)
    )
    return result


async def _keep_render(rig: PlacedSynth, run: str, name: str, **args: Any) -> None:
    out_dir = os.environ.get("P7_RENDER_DIR")
    render = await setup_call(rig.turn(run), "pcb_render", **args)
    if out_dir:
        src = (
            Path(agent_dir(rig.session.project)) / "renders" / render["png_url"].rsplit("/", 1)[-1]
        )
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, Path(out_dir) / name)


async def test_tidy_silk_synth(copper_rig: PlacedSynth, record_property: Any) -> None:
    rig = copper_rig
    await ensure_stitch(rig)
    out = await setup_call(rig.turn("run-tidy"), "pcb_tidy_silk")
    print({k: v for k, v in out.items() if k != "step_report"})
    for k in ("moved", "resized", "silk_markers_before", "silk_markers_after"):
        record_property(f"tidy_{k}", out[k])
    record_property("tidy_hidden", len(out["hidden"]))
    m = await _model(rig, "run-tidy-after")
    hidden = {h["ref"] for h in out["hidden"]}
    assert all(h["reason"] for h in out["hidden"])
    by_kiid = {k: p.ref for p in m.parts for k, role in p.item_kiids if role == "reference"}
    for p in m.parts:
        t = _ref(m, p.ref)
        if not t.visible:
            assert p.ref in hidden, p.ref
            continue
        assert abs(t.size[0] - 1.0) < 1e-6 and abs(t.size[1] - 1.0) < 1e-6, (p.ref, t)
        assert abs(t.thickness - 0.15) < 1e-6, (p.ref, t)
        assert t.layer == ("F.SilkS" if p.side == "top" else "B.SilkS"), (p.ref, t)
        upright = 90.0 if round(p.rot) % 180 == 90 else 0.0
        assert abs((t.angle - upright + 180) % 360 - 180) < 0.1, (p.ref, p.rot, t)
    drc = await _drc(rig)
    named = [
        (m_.type, by_kiid[k])
        for m_ in drc.markers
        if m_.type in silk.SILK_TYPES and m_.severity != "exclusion"
        for k in m_.kiids
        if k in by_kiid and by_kiid[k] not in hidden
    ]
    assert named == []
    metrics = await setup_call(rig.turn("run-tidy-m14"), "pcb_metrics", only=["M11", "M14"])
    for mt in metrics["metrics"]:
        print(mt["id"], mt["value"], mt["verdict"], mt["offenders"][:8])
        record_property(f"{mt['id']}_after_tidy", mt["value"])
    m14 = next(mt for mt in metrics["metrics"] if mt["id"] == "M14")
    assert not [o for o in m14["offenders"] if "reference over" in o["what"]], m14
    await _keep_render(rig, "run-tidy-top", "p7-top.png", view="top")
    await _keep_render(rig, "run-tidy-in1", "p7-in1.png", view="layer", layer="In1.Cu")


async def test_tidy_changes_board_hash(copper_rig: PlacedSynth) -> None:
    rig = copper_rig
    await _perturb(rig, "R1")
    before = await _model(rig, "run-hash-0")
    out = await setup_call(rig.turn("run-hash"), "pcb_tidy_silk")
    after = await _model(rig, "run-hash-1")
    assert out["board_hash"]["changed"] is True
    assert out["board_hash"]["after"] == board_hash(after) != board_hash(before)
    assert _ref(after, "R1").at != (after.part("R1").x, after.part("R1").y)


async def test_tidy_cost_1(copper_rig: PlacedSynth) -> None:
    cs = await silk.prepare_tidy_silk(copper_rig.turn("run-cost"), {})
    assert cs.logical.changes == 1


@pytest.mark.parametrize("n", [1, 2])
async def test_tidy_silk_fail_after(copper_rig: PlacedSynth, n: int) -> None:
    """`fail_after` 1 (after placing) and 2 (after the DRC re-plan): board_hash equal
    pre-call - the stored reference fields are written back."""
    rig = copper_rig
    await _perturb(rig, "R1")
    await _perturb(rig, "C1")
    ctx = rig.turn(f"run-fa{n}")
    before = await board_model(ctx)
    cs = await silk.prepare_tidy_silk(ctx, {})
    cs.fail_after = n
    if ctx.on_mutation is not None:
        await ctx.on_mutation()
    with pytest.raises(RuntimeError, match="fail_after"):
        await silk.apply_tidy_silk(ctx, cs)
    after = await _model(rig, f"run-fa{n}-after")
    assert board_hash(after) == board_hash(before)
    assert _ref(after, "R1") == _ref(before, "R1")
    await setup_call(rig.turn(f"run-fa{n}-tidy"), "pcb_tidy_silk")
