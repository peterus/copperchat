"""`pcb_set_rules` on the real api-server (SP4 P2 Task 3), on `sp4_synth` after a sync.

Every call goes through the real two-phase adapter (`call_tool`), which saves the board
afterwards. No KiCad file is written by hand: `.kicad_pro` / `.kicad_dru` are only read.
Also holds the two P2 probes: rules persistence (spec §6.2) and the copper layer type
(spec §10.3).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from kipy.board import Board
from kipy.board_types import Track
from kipy.geometry import Vector2
from kipy.proto.board import board_commands_pb2, board_pb2
from kipy.proto.board.board_types_pb2 import BoardLayer

from copperchat.board.model import BoardModel, board_hash
from copperchat.board.presets import PRESETS
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.board.read import load_board_model
from copperchat.tools import registry
from copperchat.tools.spec import ToolContext
from tests.kicad.board.conftest import SynthRig
from tests.support.edit import call_tool

pytestmark = pytest.mark.kicad
MM = 1_000_000

# FabPreset field -> KiCad MinimumConstraints field (plan Task 3 mapping).
RULE_OF = {
    "min_space": "min_clearance",
    "min_track": "min_track_width",
    "min_via_diameter": "min_via_size",
    "min_drill": "min_through_drill",
    "min_annular": "min_via_annular_width",
    "copper_to_edge": "copper_edge_clearance",
    "hole_to_hole": "hole_to_hole_min",
    "silk_min_height": "min_silk_text_height",
    "silk_min_line": "min_silk_text_thickness",
}

POWER = {
    "name": "Power",
    "track_width": 0.5,
    "clearance": 0.2,
    "via_diameter": 0.8,
    "via_drill": 0.4,
    "patterns": ["+3V3"],
}

RULE = {
    "name": "power_clear",
    "condition": "A.NetClass == 'Power'",
    "constraint": "clearance",
    "min": 0.3,
    "reason": "power nets keep a wider gap",
}


async def _synced(rig: SynthRig) -> ToolContext:
    ctx = rig.ctx()
    data, err = await call_tool(ctx, "pcb_sync_from_schematic")
    assert not err, data
    return rig.ctx()  # the board creation reopened the session


async def _rules(ctx: ToolContext, **args: Any) -> tuple[dict[str, Any], bool]:
    return await call_tool(ctx, "pcb_set_rules", **args)


async def _model(ctx: ToolContext) -> BoardModel:
    project = ctx.project
    return await ctx.session.call(lambda h: load_board_model(h, project), timeout=120)


def _dru(ctx: ToolContext) -> Path:
    return ctx.project.pro_file.with_suffix(".kicad_dru")


def _expected(preset: str) -> dict[str, float]:
    p = PRESETS[preset]
    return {rule: getattr(p, field) for field, rule in RULE_OF.items()}


def _assert_rules(m: BoardModel, expected: dict[str, float]) -> None:
    rules = dict(m.rules)
    for name, value in expected.items():
        assert abs(rules[name] - value) <= 0.0005, (name, rules[name], value)


async def test_preset_4l_roundtrip(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    data, err = await _rules(ctx, layers=4, preset="jlcpcb-4l")
    assert not err, data
    m = await _model(ctx)
    assert len(m.copper_layers) == 4
    _assert_rules(m, _expected("jlcpcb-4l"))
    assert data["layers"] == 4 and data["next"] == "pcb_analyze"
    assert data["board_hash_changed"] is True
    assert {r["rule"] for r in data["rules"]} >= set(RULE_OF.values())


async def test_preset_without_layers_keeps_layer_count(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    data, err = await _rules(ctx, preset="jlcpcb-4l")
    assert not err, data
    m = await _model(ctx)
    assert m.copper_layers == ("F.Cu", "B.Cu") or len(m.copper_layers) == 2
    _assert_rules(m, _expected("jlcpcb-4l"))
    assert data["layers"] == 2
    assert any("4" in n and "layers" in n for n in data["notes"]), data["notes"]


async def test_rules_persist_to_kicad_pro(synth_rig: SynthRig, record_property: Any) -> None:
    """Probe (spec §6.2): the adapter's board save persists the rules and the net
    classes into `.kicad_pro` (spike Q2)."""
    ctx = await _synced(synth_rig)
    data, err = await _rules(ctx, preset="jlcpcb-2l", net_classes=[POWER])
    assert not err, data
    pro = json.loads(ctx.project.pro_file.read_text())
    min_clearance = pro["board"]["design_settings"]["rules"]["min_clearance"]
    classes = {c["name"]: c for c in pro["net_settings"]["classes"]}
    patterns = pro["net_settings"].get("netclass_patterns") or []
    record_property("rules_min_clearance_in_kicad_pro", min_clearance)
    record_property("net_class_in_kicad_pro", "Power" in classes)
    assert min_clearance == 0.127
    assert "Power" in classes and abs(classes["Power"]["track_width"] - 0.5) < 1e-6
    assert {"netclass": "Power", "pattern": "+3V3"} in patterns, patterns


async def test_net_class_applies(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    data, err = await _rules(ctx, net_classes=[POWER])
    assert not err, data

    def class_of(h: KicadHandle) -> tuple[str, list[str]]:
        board = h.board()
        net = next(n for n in board.get_nets() if n.name == "+3V3")
        nc = board.get_netclass_for_nets([net])[net.name]
        return str(nc.name), list(nc.constituents or [])

    name, constituents = await ctx.session.call(class_of)
    assert name == "Power" or "Power" in constituents, (name, constituents)
    m = await _model(ctx)
    power = next(c for c in m.net_classes if c.name == "Power")
    assert power.track_width == 0.5 and power.patterns == ("+3V3",)
    assert "+3V3" in dict(m.net_class_of)


async def test_custom_rule_with_reason(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    assert not _dru(ctx).exists()
    data, err = await _rules(ctx, net_classes=[POWER], custom_rules=[RULE])
    assert not err, data
    text = _dru(ctx).read_text()
    assert "power_clear" in text and "power nets keep a wider gap" in text, text
    assert data["custom_rules"][0]["reason"] == RULE["reason"]
    assert data["dru_created"] is True


async def test_invalid_condition(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    before = board_hash(await _model(ctx))
    bad = dict(RULE, condition="A.NetClass ==== (")
    data, err = await _rules(ctx, custom_rules=[bad])
    assert err and data["error"]["code"] == "invalid_argument", data
    assert board_hash(await _model(ctx)) == before
    assert not os.path.lexists(_dru(ctx))


async def test_invalid_condition_keeps_existing_dru_bytes(synth_rig: SynthRig) -> None:
    """A rejected rule on a project that already has a .kicad_dru: the file is not
    re-written (KiCad would re-serialise it), the board hash is unchanged."""
    ctx = await _synced(synth_rig)
    data, err = await _rules(ctx, net_classes=[POWER], custom_rules=[RULE])
    assert not err, data
    ctx = synth_rig.ctx()
    raw = _dru(ctx).read_bytes()
    before = board_hash(await _model(ctx))
    bad = dict(RULE, name="other_rule", condition="A.NetClass ==== (")
    data, err = await _rules(ctx, custom_rules=[bad])
    assert err and data["error"]["code"] == "invalid_argument", data
    assert _dru(ctx).read_bytes() == raw
    assert board_hash(await _model(ctx)) == before


async def test_net_class_pattern_replaced_and_moved(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    data, err = await _rules(ctx, net_classes=[POWER])
    assert not err, data
    data, err = await _rules(ctx, net_classes=[dict(POWER, patterns=["VBUS"])])
    assert not err, data
    classes = {c.name: c for c in (await _model(ctx)).net_classes}
    assert set(classes["Power"].patterns) == {"VBUS"}
    other = dict(POWER, name="Usb", patterns=["VBUS"])
    data, err = await _rules(ctx, net_classes=[dict(POWER, patterns=["+3V3"]), other])
    assert not err, data
    classes = {c.name: c for c in (await _model(ctx)).net_classes}
    assert set(classes["Power"].patterns) == {"+3V3"}
    assert set(classes["Usb"].patterns) == {"VBUS"}
    # a pattern given to a new class leaves the unnamed class that had it: reported
    data, err = await _rules(ctx, net_classes=[dict(POWER, name="Rail", patterns=["+3V3"])])
    assert not err, data
    assert data["patterns_moved"] == [{"pattern": "+3V3", "from": "Power", "to": "Rail"}]
    classes = {c.name: c for c in (await _model(ctx)).net_classes}
    assert set(classes["Power"].patterns) == set() and set(classes["Rail"].patterns) == {"+3V3"}


async def _set_4(ctx: ToolContext) -> None:
    data, err = await _rules(ctx, layers=4)
    assert not err, data


async def test_4_to_2_refused_with_inner_copper(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    await _set_4(ctx)

    def add_track(h: KicadHandle) -> str:
        t = Track()
        t.layer = BoardLayer.BL_In1_Cu
        t.start = Vector2.from_xy(10 * MM, 10 * MM)
        t.end = Vector2.from_xy(20 * MM, 10 * MM)
        t.width = 250_000
        made = h.board().create_items(t)
        return str(made[0].id.value)

    async with ctx.session.commit("test: inner track", doc="pcb"):
        kiid = await ctx.session.call(add_track)
    m = await _model(ctx)
    assert kiid in {t.kiid for t in m.tracks}
    data, err = await _rules(ctx, layers=2)
    assert err and data["error"]["code"] == "invalid_argument", data
    items = data["error"]["details"]["items"]
    assert {"kind": "track", "kiid": kiid, "layer": "In1.Cu"} in items
    m = await _model(ctx)
    assert len(m.copper_layers) == 4 and kiid in {t.kiid for t in m.tracks}


async def test_4_to_2_refused_with_blind_via(synth_rig: SynthRig) -> None:
    """A blind via F.Cu-In1.Cu blocks the drop to 2 layers (KiCad would delete it); a
    through via does not."""
    from kipy.board_types import Via
    from kipy.proto.board.board_types_pb2 import ViaType

    ctx = await _synced(synth_rig)
    await _set_4(ctx)

    def add_vias(h: KicadHandle) -> tuple[str, str]:
        made = []
        for kind, x in ((ViaType.VT_THROUGH, 10), (ViaType.VT_BLIND_BURIED, 20)):
            v = Via()
            v.position = Vector2.from_xy(x * MM, 10 * MM)
            v.type = kind
            if kind == ViaType.VT_BLIND_BURIED:
                v.padstack.drill.start_layer = BoardLayer.BL_F_Cu
                v.padstack.drill.end_layer = BoardLayer.BL_In1_Cu
            v.diameter = 600_000
            v.drill_diameter = 300_000
            made.append(str(h.board().create_items([v])[0].id.value))
        return made[0], made[1]

    async with ctx.session.commit("test: vias", doc="pcb"):
        through, blind = await ctx.session.call(add_vias)
    data, err = await _rules(ctx, layers=2)
    assert err and data["error"]["code"] == "invalid_argument", data
    items = data["error"]["details"]["items"]
    assert {"kind": "via", "kiid": blind, "layer": "In1.Cu"} in items, items
    assert not [i for i in items if i["kiid"] == through], items
    m = await _model(ctx)
    assert len(m.copper_layers) == 4 and {through, blind} <= {v.kiid for v in m.vias}


async def test_4_to_2_empty_inner_ok(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    await _set_4(ctx)
    data, err = await _rules(ctx, layers=2)
    assert not err, data
    assert len((await _model(ctx)).copper_layers) == 2


async def test_fail_after_3_restores(synth_rig: SynthRig, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = await _synced(synth_rig)
    before = await _model(ctx)
    files = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in ctx.project.pro_file.parent.iterdir()
        if p.suffix == ".kicad_dru"
    }
    specs = registry.all_specs()
    spec = next(s for s in specs if s.name == "pcb_set_rules")
    assert spec.prepare is not None
    prepare = spec.prepare

    async def failing(c: ToolContext, args: dict[str, Any]) -> Any:
        cs = await prepare(c, args)
        cs.fail_after = 3
        return cs

    patched = [dataclasses.replace(s, prepare=failing) if s is spec else s for s in specs]
    monkeypatch.setattr(registry, "all_specs", lambda: patched)
    data, err = await _rules(ctx, layers=4, preset="jlcpcb-4l", net_classes=[POWER])
    assert err, data
    after = await _model(ctx)
    assert after.copper_layers == before.copper_layers
    assert after.rules == before.rules
    assert after.net_classes == before.net_classes
    assert after.net_class_of == before.net_class_of
    assert board_hash(after) == board_hash(before)
    assert {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in ctx.project.pro_file.parent.iterdir()
        if p.suffix == ".kicad_dru"
    } == files


async def test_undo_first_dru_via_history(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    dru = _dru(ctx)
    assert not os.path.lexists(dru)
    data, err = await _rules(ctx, custom_rules=[dict(RULE, condition="A.NetClass == 'Default'")])
    assert not err, data
    assert dru.is_file()
    await synth_rig.restore_turn(ctx)
    assert not os.path.lexists(dru)


def _no_set_custom(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    def record(self: Board, rules: Any) -> Any:
        calls.append(rules)
        raise AssertionError("SetCustomDesignRules must not be called")

    monkeypatch.setattr(Board, "set_custom_design_rules", record)
    return calls


async def test_symlinked_dru_refused(
    synth_rig: SynthRig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = await _synced(synth_rig)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "rules.txt"
    target.write_bytes(b"not a rules file\n")
    _dru(ctx).symlink_to(target)
    calls = _no_set_custom(monkeypatch)
    before = board_hash(await _model(ctx))
    data, err = await _rules(ctx, custom_rules=[RULE])
    assert err and data["error"]["code"] == "output_path_unsafe", data
    assert calls == []
    assert target.read_bytes() == b"not a rules file\n"
    assert sorted(p.name for p in outside.iterdir()) == ["rules.txt"]
    assert _dru(ctx).is_symlink()
    assert board_hash(await _model(ctx)) == before


async def test_dru_dir_in_the_way_refused(
    synth_rig: SynthRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = await _synced(synth_rig)
    _dru(ctx).mkdir()
    calls = _no_set_custom(monkeypatch)
    data, err = await _rules(ctx, custom_rules=[RULE])
    assert err and data["error"]["code"] == "output_path_unsafe", data
    assert calls == []
    assert _dru(ctx).is_dir() and list(_dru(ctx).iterdir()) == []


def test_probe_layer_type_power(record_property: Any) -> None:
    """Probe (spec §10.3): the pinned protos offer no settable copper layer type
    (signal / power / mixed / jumper). A KiCad bump that adds one fails this test on
    purpose: P6b can then set plane layers through the API instead of its fallbacks."""
    words = ("signal", "power", "mixed", "jumper", "layer_type")

    def named(name: str) -> bool:
        return any(w in name.lower() for w in words)

    layer = board_pb2.BoardStackupLayer.DESCRIPTOR
    assert not [f.name for f in layer.fields if named(f.name)]
    for f in layer.fields:  # `type` = copper / dielectric / mask ..., never signal / power
        if f.enum_type is not None:
            assert not [v for v in f.enum_type.values_by_name if named(v)], f.name
    setters = [
        m
        for name, m in board_commands_pb2.DESCRIPTOR.message_types_by_name.items()
        if name.startswith(("Set", "Update", "Create"))
    ]
    for m in setters:
        for f in m.fields:
            assert not named(f.name), (m.name, f.name)
            if f.enum_type is not None:
                assert not [v for v in f.enum_type.values_by_name if named(v)], (m.name, f.name)
            if f.message_type is not None and f.message_type is not layer:
                assert not [g.name for g in f.message_type.fields if named(g.name)], (
                    m.name,
                    f.name,
                )
    record_property("layer_type_settable", False)
