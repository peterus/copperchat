"""`pcb_set_outline` on the real api-server (SP4 P2 Task 2), on `sp4_synth` after a sync.

Every call goes through the real two-phase adapter (`call_tool`); footprint attributes
are read back through kipy.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from copperchat.board.model import BoardModel, board_hash, placement_hash
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.board import api, setup
from copperchat.kicad_tools.board.read import load_board_model
from copperchat.kicad_tools.checks import run_kicad_check
from copperchat.tools.spec import ToolContext
from tests.kicad.board.conftest import SynthRig
from tests.support.edit import call_tool

pytestmark = pytest.mark.kicad
TOL = 0.001
OUTLINE = {"shape": "rect", "width": 60, "height": 40, "corner_radius": 3}


async def _call(ctx: ToolContext, name: str, **args: Any) -> tuple[dict[str, Any], bool]:
    if ctx.on_mutation is not None:
        await ctx.on_mutation()  # the turn snapshot (the run engine takes it once per turn)
    return await call_tool(ctx, name, **args)


async def _outline(ctx: ToolContext, **args: Any) -> tuple[dict[str, Any], bool]:
    return await _call(ctx, "pcb_set_outline", **(OUTLINE | args))


async def _synced(rig: SynthRig) -> ToolContext:
    data, err = await _call(rig.ctx(), "pcb_sync_from_schematic")
    assert not err, data
    return rig.ctx()


async def _model(ctx: ToolContext) -> BoardModel:
    project = ctx.project
    return await ctx.session.call(lambda h: load_board_model(h, project), timeout=120)


def _hashes(m: BoardModel) -> tuple[str, str]:
    return placement_hash(m), board_hash(m)


def _holes(m: BoardModel) -> dict[str, Any]:
    return {p.ref: p for p in m.parts if p.lib_id.startswith("MountingHole")}


def _at(p: Any, target: tuple[float, float]) -> bool:
    return abs(p.x - target[0]) <= TOL and abs(p.y - target[1]) <= TOL


def _corners(inset: float) -> list[tuple[float, float]]:
    return [(100 + inset, 100 + inset), (160 - inset, 100 + inset),
            (100 + inset, 140 - inset), (160 - inset, 140 - inset)]  # fmt: skip


def _attributes(ctx: ToolContext) -> Any:
    def read(h: KicadHandle) -> dict[str, tuple[bool, bool, bool]]:
        out = {}
        for fp in h.board().get_footprints():
            a = fp.attributes
            out[str(fp.reference_field.text.value)] = (
                a.not_in_schematic,
                a.exclude_from_bill_of_materials,
                a.exclude_from_position_files,
            )
        return out

    return ctx.session.call(read, timeout=60)


async def test_rect_with_radius(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    data, err = await _outline(ctx)
    assert not err, data
    m = await _model(ctx)
    o = m.outline
    assert o.kind == "rect"
    assert [round(v, 3) for v in (o.x, o.y, o.w, o.h, o.corner_radius)] == [100, 100, 60, 40, 3]
    assert len([g for g in m.graphics if g.layer == "Edge.Cuts"]) == 1
    assert data["outline"] == {"x": 100, "y": 100, "width": 60, "height": 40, "corner_radius": 3}
    assert data["next"] == "pcb_set_rules" and data["holes"] is None and data["keepouts"] == []
    drc = await ctx.session.call(lambda h: api.run_drc_task(h), timeout=api.drc_call_timeout(60))
    assert not [mk for mk in drc.markers if mk.type == "invalid_outline"]


async def test_schematic_holes_moved_not_duplicated(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    data, err = await _outline(ctx, holes={})
    assert not err, data
    m = await _model(ctx)
    holes = _holes(m)
    assert len(holes) == 4, sorted(holes)
    targets = _corners(3.0)
    assert _at(holes["H1"], targets[0]) and _at(holes["H2"], targets[1])
    assert holes["H1"].symbol_path and holes["H2"].symbol_path
    assert all(p.locked for p in holes.values())
    tool = sorted(r for r, p in holes.items() if not p.symbol_path)
    assert tool == ["H3", "H4"]
    assert {(round(holes[r].x, 3), round(holes[r].y, 3)) for r in tool} == set(targets[2:])
    assert all(holes[r].group is None for r in tool)
    assert data["holes"] == {
        "moved": ["H1", "H2"],
        "placed": ["H3", "H4"],
        "removed": [],
        "kept": [],
    }


async def test_rerun_replaces_tool_holes(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    data, err = await _outline(ctx, holes={})
    assert not err, data
    first = _holes(await _model(ctx))
    data, err = await _outline(synth_rig.ctx(), holes={"inset": 4})
    assert not err, data
    holes = _holes(await _model(ctx))
    assert len(holes) == 4, sorted(holes)
    targets = _corners(4.0)
    for i, ref in enumerate(["H1", "H2", "H3", "H4"]):
        assert _at(holes[ref], targets[i]), ref
    assert holes["H1"].kiid == first["H1"].kiid and holes["H2"].kiid == first["H2"].kiid
    assert {holes[r].kiid for r in ("H3", "H4")}.isdisjoint({first[r].kiid for r in ("H3", "H4")})
    assert data["holes"] == {
        "moved": ["H1", "H2"],
        "placed": ["H3", "H4"],
        "removed": ["H3", "H4"],
        "kept": [],
    }


async def test_hand_placed_hole_kept(synth_rig: SynthRig) -> None:
    """A symbol-less hole without the tool's attributes (placed by the user) is never
    removed by a re-run."""
    from kipy.geometry import Vector2

    ctx = await _synced(synth_rig)

    def place(h: KicadHandle) -> str:
        board = h.board()
        h.kicad.get_library_footprint(setup.DEFAULT_HOLE, board.document)
        fp = board.place_footprint_from_library(
            setup.DEFAULT_HOLE, Vector2.from_xy(130_000_000, 120_000_000)
        )
        return str(fp.id.value)

    async with ctx.session.commit("test: user hole", doc="pcb"):
        kiid = await ctx.session.call(place, timeout=120)
    data, err = await _outline(synth_rig.ctx(), holes={})
    assert not err, data
    data, err = await _outline(synth_rig.ctx(), holes={"inset": 4})
    assert not err, data
    kiids = {p.kiid for p in (await _model(ctx)).parts}
    assert kiid in kiids
    assert len(data["holes"]["kept"]) == 1, data["holes"]


async def test_no_holes_arg_keeps_holes(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    data, err = await _outline(ctx, holes={})
    assert not err, data
    before = {r: (p.kiid, p.x, p.y, p.locked) for r, p in _holes(await _model(ctx)).items()}
    data, err = await _outline(synth_rig.ctx(), corner_radius=1)
    assert not err, data
    after = {r: (p.kiid, p.x, p.y, p.locked) for r, p in _holes(await _model(ctx)).items()}
    assert after == before
    assert data["holes"] is None


async def test_tool_hole_attributes(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    before = await _attributes(ctx)
    data, err = await _outline(ctx, holes={})
    assert not err, data
    after = await _attributes(ctx)
    for ref in data["holes"]["placed"]:
        assert after[ref] == (True, True, True), ref
    for ref in ("H1", "H2"):
        assert after[ref] == before[ref], ref


async def test_parity_clean_after_set_outline(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    data, err = await _outline(ctx, holes={})
    assert not err, data
    refs = set(_holes(await _model(ctx)))
    report = json.loads(await run_kicad_check(ctx.session, "pcb", ctx.settings))
    assert "schematic_parity" in report, sorted(report)
    parity = report["schematic_parity"]
    print("parity entries:", [v.get("description") for v in parity])
    texts = [json.dumps(v) for v in parity]
    hits = [t for t in texts if set(re.findall(r"\bH\d+\b", t)) & refs]
    assert hits == [], hits


async def test_resync_keeps_tool_holes(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    data, err = await _outline(ctx, holes={})
    assert not err, data
    tool = {r: (p.kiid, round(p.x, 3), round(p.y, 3)) for r, p in _holes(await _model(ctx)).items()
            if not p.symbol_path}  # fmt: skip
    assert len(tool) == 2
    data, err = await _call(synth_rig.ctx(), "pcb_sync_from_schematic")
    assert not err, data
    holes = _holes(await _model(synth_rig.ctx()))
    now = {r: (round(p.x, 3), round(p.y, 3)) for r, p in holes.items() if not p.symbol_path}
    assert now == {r: v[1:] for r, v in tool.items()}
    assert all(holes[r].locked for r in now)
    print("resync kept tool holes by KIID:", {holes[r].kiid for r in now} == {
        v[0] for v in tool.values()})  # fmt: skip


async def test_keepout_rule_area(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    keepouts = [
        {"name": "ANT KEEPOUT", "rect": {"x": 140, "y": 100, "w": 20, "h": 10},
         "layers": ["all_copper"], "no": ["tracks", "vias", "copper"]},
        {"name": "front", "polygon": [{"x": 102, "y": 120}, {"x": 110, "y": 120},
         {"x": 106, "y": 130}], "layers": ["F.Cu"], "no": ["footprints"]},
    ]  # fmt: skip
    data, err = await _outline(ctx, keepouts=keepouts)
    assert not err, data
    m = await _model(ctx)
    zones = {z.name: z for z in m.zones if z.rule_area}
    ant = zones["ANT KEEPOUT"]
    assert ant.keepout == frozenset({"tracks", "vias", "copper"})
    assert set(ant.layers) == set(m.copper_layers)
    front = zones["front"]
    assert front.keepout == frozenset({"footprints"}) and front.layers == ("F.Cu",)
    assert data["keepouts"] == ["ANT KEEPOUT", "front"]
    # a rerun replaces the named rule area, other rule areas stay
    again = [keepouts[0] | {"no": ["tracks"]}]
    data, err = await _outline(synth_rig.ctx(), keepouts=again)
    assert not err, data
    zones2 = [z for z in (await _model(ctx)).zones if z.rule_area]
    assert sorted(z.name for z in zones2) == ["ANT KEEPOUT", "front"]
    assert next(z for z in zones2 if z.name == "ANT KEEPOUT").keepout == frozenset({"tracks"})


async def test_unknown_keepout_layer(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    before = _hashes(await _model(ctx))
    bad = [{"name": "k", "rect": {"x": 101, "y": 101, "w": 5, "h": 5}, "layers": ["In7.Cu"],
            "no": ["tracks"]}]  # fmt: skip
    data, err = await _outline(ctx, keepouts=bad)
    assert err and data["error"]["code"] == "invalid_argument", data
    assert _hashes(await _model(ctx)) == before


async def test_outline_changes_placement_hash(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    before = placement_hash(await _model(ctx))
    data, err = await _outline(ctx)
    assert not err, data
    after = placement_hash(await _model(ctx))
    assert after != before
    assert data["placement_hash"] == {"before": before, "after": after, "changed": True}
    assert data["unplaced"] > 0


async def test_unknown_hole_footprint(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    before = _hashes(await _model(ctx))
    data, err = await _outline(ctx, holes={"footprint": "MountingHole:MountingHole_2.2mm_M9"})
    assert err and data["error"]["code"] == "not_found", data
    assert data["error"]["details"]["closest"], data
    assert _hashes(await _model(ctx)) == before


@pytest.mark.parametrize("n", [1, 2])
async def test_fail_after(synth_rig: SynthRig, monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    ctx = await _synced(synth_rig)
    before = _hashes(await _model(ctx))
    real = setup.apply_board_change_set

    async def failing(ctx: ToolContext, cs: Any) -> Any:
        cs.fail_after = n
        return await real(ctx, cs)

    monkeypatch.setattr(setup, "apply_board_change_set", failing)
    keepouts = [{"name": "k", "rect": {"x": 101, "y": 101, "w": 5, "h": 5},
                 "layers": ["all_copper"], "no": ["tracks"]}]  # fmt: skip
    data, err = await _outline(ctx, holes={}, keepouts=keepouts)
    assert err, data
    print(f"fail_after={n}:", data["error"])
    assert data["error"].get("code") != "document_inconsistent", data
    assert _hashes(await _model(ctx)) == before


@pytest.mark.parametrize("n", [1, 2, 3])
async def test_rerun_fail_after_restores(
    synth_rig: SynthRig, monkeypatch: pytest.MonkeyPatch, n: int
) -> None:
    """A failing re-run (old outline, tool holes and a same-named keepout to replace)
    leaves the board as it was: every deletion sits in the last stage."""
    ctx = await _synced(synth_rig)
    keepouts = [{"name": "k", "rect": {"x": 101, "y": 101, "w": 5, "h": 5},
                 "layers": ["all_copper"], "no": ["tracks"]}]  # fmt: skip
    data, err = await _outline(ctx, holes={}, keepouts=keepouts)
    assert not err, data
    ctx = synth_rig.ctx()
    before = _hashes(await _model(ctx))
    real = setup.apply_board_change_set

    async def failing(ctx: ToolContext, cs: Any) -> Any:
        cs.fail_after = n
        return await real(ctx, cs)

    monkeypatch.setattr(setup, "apply_board_change_set", failing)
    keepouts[0]["rect"] = {"x": 110, "y": 110, "w": 5, "h": 5}
    data, err = await _outline(ctx, width=70, holes={"inset": 4}, keepouts=keepouts)
    assert err, data
    assert data["error"].get("code") != "document_inconsistent", data
    assert _hashes(await _model(ctx)) == before


async def test_undo_via_history(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    before = _hashes(await _model(ctx))
    data, err = await _outline(ctx, holes={})
    assert not err, data
    await synth_rig.restore_turn(ctx)
    m = await _model(synth_rig.ctx())
    assert m.outline.kind == "none"
    assert not [g for g in m.graphics if g.layer == "Edge.Cuts"]
    assert _hashes(m) == before
