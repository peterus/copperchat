"""Input schemas of the board setup tools (SP4 P2 Tasks 2 and 3): bounds (plan call 6),
`additionalProperties: false` at every level, and the checks JSON Schema cannot express
(done by the pure argument parsers before the board is touched)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.board.setup import hole_targets, parse_outline_args
from copperchat.kicad_tools.board.specs_setup import PCB_SET_OUTLINE_SCHEMA, SETUP_SPECS

# -- pcb_set_outline ---------------------------------------------------------------------


def _outline_ok() -> dict[str, Any]:
    return {
        "shape": "rect",
        "width": 60,
        "height": 40,
        "corner_radius": 3,
        "origin": {"x": 100, "y": 100},
        "holes": {
            "footprint": "MountingHole:MountingHole_2.2mm_M2",
            "inset": 3.0,
            "positions": [{"x": 105, "y": 105}],
        },
        "keepouts": [
            {
                "name": "ANT KEEPOUT",
                "rect": {"x": 140, "y": 100, "w": 20, "h": 10},
                "layers": ["all_copper"],
                "no": ["tracks", "vias", "copper"],
            },
            {
                "name": "mech",
                "polygon": [{"x": 100, "y": 120}, {"x": 110, "y": 120}, {"x": 105, "y": 130}],
                "layers": ["F.Cu"],
                "no": ["footprints"],
            },
        ],
    }


def _outline_errors(args: dict[str, Any]) -> list[str]:
    v = Draft202012Validator(PCB_SET_OUTLINE_SCHEMA)
    return [e.message for e in v.iter_errors(args)]


def test_outline_registered() -> None:
    spec = next(s for s in SETUP_SPECS if s.name == "pcb_set_outline")
    assert spec.input_schema is PCB_SET_OUTLINE_SCHEMA
    assert spec.mutating and spec.changes_documents and spec.prepare and spec.apply
    Draft202012Validator.check_schema(PCB_SET_OUTLINE_SCHEMA)


def test_outline_valid_example() -> None:
    assert _outline_errors(_outline_ok()) == []
    assert _outline_errors({"shape": "rect", "width": 5, "height": 500}) == []


def _with(path: list[Any], value: Any) -> dict[str, Any]:
    args = _outline_ok()
    node: Any = args
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return args


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (["width"], 4),
        (["height"], 501),
        (["shape"], "circle"),
        (["corner_radius"], -1),
        (["origin", "x"], 1001),
        (["holes", "inset"], 0.4),
        (["holes", "inset"], 51),
        (["holes", "footprint"], "M" * 129),
        (["holes", "positions"], [{"x": 1, "y": 1}] * 17),
        (["keepouts", 0, "name"], "a/b"),
        (["keepouts", 0, "name"], " lead"),
        (["keepouts", 0, "no"], ["tracks", "pads"]),
        (["keepouts", 0, "no"], []),
        (["keepouts", 0, "layers"], ["F.Cu"] * 33),
        (["keepouts", 1, "polygon"], [{"x": 1, "y": 1}] * 257),
        (["keepouts", 1, "polygon"], [{"x": 1, "y": 1}] * 2),
        (["keepouts", 0, "rect", "w"], 0),
        (["keepouts"], [_outline_ok()["keepouts"][0]] * 17),
    ],
)
def test_outline_bounds_rejected(path: list[Any], value: Any) -> None:
    assert _outline_errors(_with(path, value)), (path, value)


@pytest.mark.parametrize(
    "path",
    [[], ["origin"], ["holes"], ["holes", "positions", 0], ["keepouts", 0], ["keepouts", 0, "rect"],
     ["keepouts", 1, "polygon", 0]],
)  # fmt: skip
def test_outline_additional_properties_rejected(path: list[Any]) -> None:
    args = _outline_ok()
    node: Any = args
    for key in path:
        node = node[key]
    node["extra"] = 1
    assert _outline_errors(args), path


def _objects(schema: Any) -> list[Any]:
    if not isinstance(schema, dict):
        return []
    out = [schema] if schema.get("type") == "object" else []
    for sub in (schema.get("properties") or {}).values():
        out += _objects(sub)
    out += _objects(schema.get("items"))
    return out


def test_outline_every_object_closed() -> None:
    objs = _objects(PCB_SET_OUTLINE_SCHEMA)
    assert len(objs) >= 6
    assert all(o.get("additionalProperties") is False for o in objs)


def _parse_error(args: dict[str, Any]) -> StructuredToolError:
    with pytest.raises(StructuredToolError) as info:
        parse_outline_args(args)
    assert info.value.code == "invalid_argument"
    return info.value


def test_outline_corner_radius_above_half_side() -> None:
    _parse_error({"shape": "rect", "width": 60, "height": 40, "corner_radius": 20.01})
    parsed = parse_outline_args({"shape": "rect", "width": 60, "height": 40, "corner_radius": 20})
    assert parsed.corner_radius == 20


def test_outline_keepout_needs_one_shape() -> None:
    both = _outline_ok()
    both["keepouts"][0]["polygon"] = both["keepouts"][1]["polygon"]
    _parse_error(both)
    neither = _outline_ok()
    del neither["keepouts"][0]["rect"]
    _parse_error(neither)


def test_outline_duplicate_keepout_names() -> None:
    args = _outline_ok()
    args["keepouts"][1]["name"] = "ANT KEEPOUT"
    _parse_error(args)


def test_outline_inset_too_large() -> None:
    _parse_error({"shape": "rect", "width": 10, "height": 40, "holes": {"inset": 5}})


def test_outline_hole_position_outside() -> None:
    _parse_error(_with(["holes", "positions"], [{"x": 99, "y": 105}]))


def test_outline_defaults_and_corner_targets() -> None:
    p = parse_outline_args({"shape": "rect", "width": 60, "height": 40, "holes": {}})
    assert (p.x, p.y, p.w, p.h, p.corner_radius) == (100, 100, 60, 40, 0)
    assert p.holes is not None and p.holes.footprint == "MountingHole:MountingHole_2.2mm_M2"
    assert hole_targets(p) == [(103, 103), (157, 103), (103, 137), (157, 137)]
    assert parse_outline_args({"shape": "rect", "width": 60, "height": 40}).holes is None
    k = parse_outline_args(_outline_ok()).keepouts
    assert k[0].polygon == ((140, 100), (160, 100), (160, 110), (140, 110))
    assert k[0].no == frozenset({"tracks", "vias", "copper"})


# -- pcb_set_rules -----------------------------------------------------------------------


def _rules_ok() -> dict[str, Any]:
    return {
        "layers": 4,
        "preset": "jlcpcb-4l",
        "overrides": {"min_space": 0.15, "copper_to_edge": 0.5},
        "net_classes": [
            {
                "name": "Power",
                "track_width": 0.5,
                "clearance": 0.2,
                "via_diameter": 0.8,
                "via_drill": 0.4,
                "diff_pair_width": 0.2,
                "diff_pair_gap": 0.15,
                "patterns": ["+3V3", "+5V*"],
            }
        ],
        "custom_rules": [
            {
                "name": "ant clearance",
                "condition": "A.NetClass == 'Power'",
                "constraint": "clearance",
                "min": 0.3,
                "reason": "antenna feed needs more room",
            }
        ],
    }


def _rules_errors(args: dict[str, Any]) -> list[str]:
    from copperchat.kicad_tools.board.specs_setup import PCB_SET_RULES_SCHEMA

    return [e.message for e in Draft202012Validator(PCB_SET_RULES_SCHEMA).iter_errors(args)]


def _rules_with(path: list[Any], value: Any) -> dict[str, Any]:
    args = _rules_ok()
    node: Any = args
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return args


def _rules_refused(args: dict[str, Any], code: str = "invalid_argument") -> StructuredToolError:
    from copperchat.kicad_tools.board.setup import parse_rules_args

    with pytest.raises(StructuredToolError) as info:
        parse_rules_args(args)
    assert info.value.code == code, info.value
    return info.value


def test_rules_registered() -> None:
    from copperchat.kicad_tools.board.specs_setup import PCB_SET_RULES_SCHEMA

    spec = next(s for s in SETUP_SPECS if s.name == "pcb_set_rules")
    assert spec.input_schema is PCB_SET_RULES_SCHEMA
    assert spec.mutating and spec.changes_documents and spec.prepare and spec.apply
    Draft202012Validator.check_schema(PCB_SET_RULES_SCHEMA)
    objs = _objects(PCB_SET_RULES_SCHEMA)
    assert len(objs) >= 4
    assert all(o.get("additionalProperties") is False for o in objs)


def test_rules_valid_example() -> None:
    from copperchat.kicad_tools.board.setup import parse_rules_args

    assert _rules_errors(_rules_ok()) == []
    req = parse_rules_args(_rules_ok())
    assert req.layers == 4 and req.preset == "jlcpcb-4l"
    assert req.overrides == {"min_space": 0.15, "copper_to_edge": 0.5}
    assert req.net_classes[0].patterns == ("+3V3", "+5V*")
    assert req.custom_rules[0].reason == "antenna feed needs more room"
    assert parse_rules_args({"layers": 2}).preset is None


def test_override_below_preset_refused() -> None:
    from copperchat.kicad_tools.board.setup import design_rule_values, parse_rules_args

    req = parse_rules_args({"preset": "jlcpcb-2l", "overrides": {"min_track": 0.1}})
    with pytest.raises(StructuredToolError) as info:
        design_rule_values(req, 2)
    assert info.value.code == "invalid_argument"
    assert "custom_rules" in f"{info.value.why} {info.value.suggestion}"
    # equal to / above the preset minimum: used instead of the preset value
    req = parse_rules_args({"preset": "jlcpcb-2l", "overrides": {"min_track": 0.2}})
    preset, values = design_rule_values(req, 2)
    assert preset.id == "jlcpcb-2l"
    assert values["min_track_width"] == 0.2 and values["min_clearance"] == 0.127
    assert values["min_via_size"] == 0.66  # call 8: 2-layer via diameter


def test_overrides_without_preset_use_the_layer_count_preset() -> None:
    from copperchat.kicad_tools.board.setup import design_rule_values, parse_rules_args

    req = parse_rules_args({"overrides": {"min_annular": 0.16}})
    preset, values = design_rule_values(req, 4)
    assert preset.id == "jlcpcb-4l" and values == {"min_via_annular_width": 0.16}
    with pytest.raises(StructuredToolError):  # 2-layer floor is 0.18
        design_rule_values(req, 2)


def test_preset_maps_every_rule() -> None:
    from copperchat.kicad_tools.board.setup import design_rule_values, parse_rules_args

    _, values = design_rule_values(parse_rules_args({"preset": "jlcpcb-4l"}), 2)
    assert values == {
        "min_clearance": 0.127,
        "min_track_width": 0.127,
        "min_via_size": 0.6,
        "min_through_drill": 0.3,
        "min_via_annular_width": 0.15,
        "copper_edge_clearance": 0.3,
        "hole_to_hole_min": 0.45,
        "min_silk_text_height": 1.0,
        "min_silk_text_thickness": 0.15,
    }


def test_custom_rule_needs_reason() -> None:
    no_reason = _rules_ok()
    del no_reason["custom_rules"][0]["reason"]
    assert _rules_errors(no_reason)
    _rules_refused(no_reason)
    short = _rules_with(["custom_rules", 0, "reason"], "7 chars")
    assert _rules_errors(short)
    _rules_refused(short)
    blank = _rules_with(["custom_rules", 0, "reason"], "        ")
    _rules_refused(blank)


def test_custom_rule_needs_min_or_max() -> None:
    args = _rules_ok()
    del args["custom_rules"][0]["min"]
    _rules_refused(args)
    args["custom_rules"][0].update({"min": 0.5, "max": 0.4})
    _rules_refused(args)


def test_rules_need_one_field() -> None:
    _rules_refused({})


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (["layers"], 3),
        (["layers"], 8),
        (["preset"], "jlcpcb-8l"),
        (["overrides", "min_space"], 0),
        (["overrides", "min_space"], 10.01),
        (["overrides", "pref_track"], 0.2),
        (["net_classes"], [_rules_ok()["net_classes"][0]] * 17),
        (["net_classes", 0, "name"], "a/b"),
        (["net_classes", 0, "track_width"], 0),
        (["net_classes", 0, "clearance"], 100.5),
        (["net_classes", 0, "via_drill"], 10.5),
        (["net_classes", 0, "patterns"], []),
        (["net_classes", 0, "patterns"], ["N*"] * 51),
        (["net_classes", 0, "patterns"], ["N" * 257]),
        (["custom_rules"], [_rules_ok()["custom_rules"][0]] * 33),
        (["custom_rules", 0, "name"], "-lead"),
        (["custom_rules", 0, "condition"], "A" * 513),
        (["custom_rules", 0, "condition"], ""),
        (["custom_rules", 0, "constraint"], "length"),
        (["custom_rules", 0, "min"], -0.1),
        (["custom_rules", 0, "max"], 101),
        (["custom_rules", 0, "reason"], "R" * 301),
        (["stackup"], "s" * 65),
    ],
)
def test_rules_bounds_rejected(path: list[Any], value: Any) -> None:
    args = _rules_with(path, value)
    assert _rules_errors(args), (path, value)
    _rules_refused(args)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (["custom_rules", 0, "condition"], "A.NetClass == 'x'\n(rule evil)"),
        (["custom_rules", 0, "reason"], "line one\nline two"),
        (["net_classes", 0, "patterns"], ["ok", "bad\x00"]),
        (["net_classes", 0, "via_drill"], 0.8),  # drill not smaller than the via
        (["net_classes"], [_rules_ok()["net_classes"][0]] * 2),  # duplicate name
        (["custom_rules"], [_rules_ok()["custom_rules"][0]] * 2),  # duplicate name
        (["custom_rules", 0, "condition"], 'A.NetClass == "x") (severity ignore'),
        (["custom_rules", 0, "reason"], "a reason with a \\ backslash"),
        (["custom_rules", 0, "reason"], 'a "quoted" reason here'),
    ],
)
def test_rules_semantic_refused(path: list[Any], value: Any) -> None:
    _rules_refused(_rules_with(path, value))


@pytest.mark.parametrize(
    "path",
    [[], ["overrides"], ["net_classes", 0], ["custom_rules", 0]],
)
def test_rules_additional_properties_rejected(path: list[Any]) -> None:
    args = _rules_ok()
    node: Any = args
    for key in path:
        node = node[key]
    node["extra"] = 1
    assert _rules_errors(args), path
    _rules_refused(args)


@pytest.mark.parametrize("value", [True, "0.2", float("nan"), float("inf")])
def test_rules_non_numbers_refused(value: Any) -> None:
    _rules_refused(_rules_with(["overrides", "min_space"], value))


def test_rules_name_pattern_is_the_shared_one() -> None:
    from copperchat.kicad_tools.board.setup import RULE_NAME
    from copperchat.kicad_tools.board.specs_setup import NAME_PATTERN

    assert RULE_NAME.pattern == NAME_PATTERN


def test_rules_layers_float_normalised() -> None:
    from copperchat.kicad_tools.board.setup import parse_rules_args

    req = parse_rules_args({"layers": 4.0})
    assert req.layers == 4 and isinstance(req.layers, int)
    _rules_refused({"layers": 4.5})


def test_custom_rule_head_matches_exact_name() -> None:
    import re

    from copperchat.kicad_tools.board.setup import _RULE_HEAD

    text = '(version 1)\n(rule "a b"\n\t(constraint clearance (min 0.3mm)))\n(rule c\n)'
    assert not re.search(_RULE_HEAD.format(re.escape("a")), text)
    assert re.search(_RULE_HEAD.format(re.escape("a b")), text)
    assert re.search(_RULE_HEAD.format(re.escape("c")), text)


def test_verify_rules_flags_pattern_mismatch() -> None:
    from copperchat.kicad_tools.board.setup import _verify_rules, parse_rules_args
    from tests.unit.board.builders import board, net_class

    req = parse_rules_args(
        {
            "net_classes": [
                {
                    "name": "Power",
                    "track_width": 0.2,
                    "clearance": 0.2,
                    "via_diameter": 0.6,
                    "via_drill": 0.3,
                    "patterns": ["+3V3"],
                }
            ]
        }
    )
    verify = _verify_rules(req, {}, "h")
    ok = board(net_classes=(net_class("Power", patterns=("+3V3",)),))
    wrong = board(net_classes=(net_class("Power", patterns=("VBUS",)),))
    assert verify(ok, {}) == []
    assert any("patterns" in p for p in verify(wrong, {}))


def test_custom_rules_undo_armed_when_the_write_raises(tmp_path: Path) -> None:
    """A rules write that raises (timeout, ApiError) keeps the undo armed; an explicit
    rejection disarms it."""
    from types import SimpleNamespace

    from kipy.proto.board.board_commands_pb2 import CustomRulesStatus

    from copperchat.kicad_tools.board.setup import CustomRuleArgs, _set_custom_rules

    pro = tmp_path / "p.kicad_pro"
    pro.write_text("{}")
    project = SimpleNamespace(pro_file=pro)
    rule = CustomRuleArgs("r1", "A.NetClass == 'x'", "clearance", 0.3, None, "a good reason")

    class Board:
        def __init__(self, outcome: Any) -> None:
            self.outcome = outcome

        def get_custom_design_rules(self) -> Any:
            return SimpleNamespace(status=CustomRulesStatus.CRS_VALID, rules=[], error_text="")

        def set_custom_design_rules(self, rules: Any) -> Any:
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return SimpleNamespace(status=self.outcome, error_text="bad")

    state: dict[str, Any] = {}
    h: Any = SimpleNamespace(board=lambda: Board(TimeoutError("slow")))
    with pytest.raises(TimeoutError):
        _set_custom_rules(h, project, (rule,), state)  # type: ignore[arg-type]
    assert state["custom"] == [] and state["dru_created"] is True

    state = {}
    h = SimpleNamespace(board=lambda: Board(CustomRulesStatus.CRS_INVALID))
    with pytest.raises(StructuredToolError):
        _set_custom_rules(h, project, (rule,), state)  # type: ignore[arg-type]
    assert "custom" not in state


def test_names_with_trailing_newline_refused() -> None:
    from copperchat.kicad_tools.board.setup import parse_outline_args

    args = _rules_ok()
    args["net_classes"][0]["name"] = "Power\n"
    _rules_refused(args)
    keepout = {"name": "k\n", "rect": {"x": 1, "y": 1, "w": 2, "h": 2}, "layers": ["F.Cu"],
               "no": ["tracks"]}  # fmt: skip
    with pytest.raises(StructuredToolError):
        parse_outline_args({"shape": "rect", "width": 50, "height": 40, "keepouts": [keepout]})


def test_same_pattern_in_two_classes_refused() -> None:
    args = _rules_ok()
    twin = dict(args["net_classes"][0], name="Other")
    args["net_classes"] = [args["net_classes"][0], twin]
    _rules_refused(args)


def test_verify_rules_flags_changed_condition() -> None:
    from copperchat.kicad_tools.board.setup import _verify_rules, parse_rules_args
    from tests.unit.board.builders import board

    rule = {
        "name": "r1",
        "condition": "A.NetClass == 'Power'",
        "constraint": "clearance",
        "min": 0.3,
        "reason": "power needs more room",
    }
    verify = _verify_rules(parse_rules_args({"custom_rules": [rule]}), {}, "h")
    good = "(version 1)\n(rule r1\n\t(condition \"A.NetClass == 'Power'\")\n)\n(rule r2\n)"
    stale = (
        "(version 1)\n(rule r1\n\t(condition \"A.NetClass == 'Old'\")\n)\n"
        "(rule r2\n\t(condition \"A.NetClass == 'Power'\"))"
    )
    assert verify(board(dru_text=good), {}) == []
    assert any("condition" in p for p in verify(board(dru_text=stale), {}))


async def test_undo_removes_created_dru_when_restore_raises(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from copperchat.kicad_tools.board.setup import _undo_custom_rules

    pro = tmp_path / "p.kicad_pro"
    pro.write_text("{}")
    dru = tmp_path / "p.kicad_dru"
    dru.write_text("(version 1)")

    async def call(fn: Any, **kw: Any) -> Any:
        raise TimeoutError("restore timed out")

    sctx: Any = SimpleNamespace(
        call=call, ctx=SimpleNamespace(project=SimpleNamespace(pro_file=pro))
    )
    with pytest.raises(TimeoutError):
        await _undo_custom_rules(sctx, {"custom": [], "dru_created": True})
    assert not dru.exists()
