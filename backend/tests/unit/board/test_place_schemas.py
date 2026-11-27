"""The four placement tool schemas (SP4 P4 Task 3/4): registered, two-phase, bounded,
`additionalProperties: false` at every level; argument checks the schema cannot express
refuse with `invalid_argument` before the board is touched."""

from __future__ import annotations

from typing import Any

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.board import place
from copperchat.kicad_tools.board.specs import TOOL_SPECS
from copperchat.kicad_tools.board.specs_place import PLACE_SPECS

from .builders_circuit import cap, circuit
from .test_schema_bounds import unbounded

NAMES = ("pcb_define_blocks", "pcb_place_block", "pcb_move", "pcb_legalize")


def _spec(name: str) -> Any:
    return next(s for s in PLACE_SPECS if s.name == name)


def _objects(schema: Any, path: str = "$") -> list[tuple[str, dict[str, Any]]]:
    if not isinstance(schema, dict):
        return []
    out = [(path, schema)] if schema.get("type") == "object" else []
    for name, sub in (schema.get("properties") or {}).items():
        out += _objects(sub, f"{path}.{name}")
    out += _objects(schema.get("items"), f"{path}[]")
    return out


def test_registered_two_phase() -> None:
    names = [s.name for s in TOOL_SPECS]
    for n in NAMES:
        assert names.count(n) == 1, n
        s = _spec(n)
        assert s.mutating and s.changes_documents
        assert s.prepare is not None and s.apply is not None


@pytest.mark.parametrize("name", NAMES)
def test_bounded(name: str) -> None:
    assert unbounded(_spec(name).input_schema) == []


@pytest.mark.parametrize("name", NAMES)
def test_additional_properties_false_everywhere(name: str) -> None:
    objs = _objects(_spec(name).input_schema)
    assert objs
    for path, o in objs:
        assert o.get("additionalProperties") is False, path


def test_limits() -> None:
    blocks = _spec("pcb_define_blocks").input_schema["properties"]["blocks"]
    assert (blocks["minItems"], blocks["maxItems"]) == (1, 32)
    refs = blocks["items"]["properties"]["refs"]
    assert (refs["minItems"], refs["maxItems"]) == (1, 200)
    assert refs["items"]["maxLength"] == 32
    items = _spec("pcb_move").input_schema["properties"]["items"]
    assert (items["minItems"], items["maxItems"]) == (1, 200)
    props = _spec("pcb_place_block").input_schema["properties"]
    assert props["fixed"]["maxItems"] == 200
    assert (props["keep_channels"]["minimum"], props["keep_channels"]["maximum"]) == (0, 5)
    assert props["region"]["properties"]["offset"]["maximum"] == 500
    assert props["rotation"]["enum"] == [0, 90, 180, 270]
    shift = _spec("pcb_legalize").input_schema["properties"]["max_shift"]
    assert (shift["exclusiveMinimum"], shift["maximum"]) == (0, 50)


def test_descriptions_name_next_step() -> None:
    assert "pcb_place_block" in _spec("pcb_define_blocks").description
    for n in NAMES[1:]:
        assert "next" in _spec(n).description


# -- argument checks (pure; before the board is read) --------------------------------------


def _block(name: str, refs: list[str], **kw: Any) -> dict[str, Any]:
    return {"name": name, "refs": refs, **kw}


@pytest.mark.parametrize(
    "args",
    [
        {"blocks": []},
        {"blocks": [_block("a", ["R1"]), _block("b", ["R1"])]},  # one ref, two blocks
        {"blocks": [_block("a", ["R1", "R1"])]},
        {"blocks": [_block("a", ["R1"], anchor="U1")]},  # anchor not in refs
        {"blocks": [_block("a", ["R1"]), _block("a", ["R2"])]},  # duplicate name
        {"blocks": [_block("bad\nname", ["R1"])]},
        {"blocks": [_block("a", ["R1"])], "replace": "yes"},
        {"blocks": [_block("a", ["R1"])], "extra": 1},
        {"blocks": [_block("a", ["R" * 33])]},
    ],
)
def test_define_args_refused(args: dict[str, Any]) -> None:
    with pytest.raises(StructuredToolError) as err:
        place.parse_define_args(args)
    assert err.value.code == "invalid_argument"


def test_define_args_ok() -> None:
    blocks, replace = place.parse_define_args(
        {"blocks": [_block("Power-U3", ["U3", "C7"], anchor="U3")], "replace": True}
    )
    assert replace is True
    assert blocks == (place.BlockArgs("Power-U3", ("U3", "C7"), "U3"),)


@pytest.mark.parametrize(
    "region",
    [
        {"x": 0, "y": 0, "w": 10},  # incomplete
        {"x": 0, "y": 0, "w": 0, "h": 10},  # zero width
        {"edge": "middle"},
        {"edge": "left", "x": 0},  # mixed forms
        {"edge": "left", "offset": 501},
        {"x": 0, "y": 0, "w": 10, "h": float("inf")},
    ],
)
def test_region_refused(region: dict[str, Any]) -> None:
    with pytest.raises(StructuredToolError) as err:
        place._parse_region(region)
    assert err.value.code == "invalid_argument"


def test_region_forms() -> None:
    rect, edge, offset = place._parse_region({"x": 1, "y": 2, "w": 3, "h": 4})
    assert rect == place.Rect(1, 2, 3, 4) and edge is None and offset is None
    assert place._parse_region({"edge": "left", "offset": 7}) == (None, "left", 7.0)


def test_moves_part_uses_hash_rounding() -> None:
    from tests.unit.board.builders import part

    p = part("R1", 10.0, 10.0)
    m = place.Move
    assert not place.moves_part(p, m("R1", 10.004, 10.0, 0.0, "top"))
    assert place.moves_part(p, m("R1", 10.01, 10.0, 0.0, "top"))
    assert not place.moves_part(p, m("R1", 10.0, 10.0, 0.04, "top"))
    assert place.moves_part(p, m("R1", 10.0, 10.0, 0.1, "top"))
    assert place.moves_part(p, m("R1", 10.0, 10.0, 0.0, "bottom"))


def test_plan_blocks() -> None:
    rows = [
        place._GroupRow("g1", "blk:a", ("k1", "k2", "track")),  # reused, keeps its track
        place._GroupRow("g2", "blk:b", ("k3", "k4")),  # loses k3
        place._GroupRow("g3", "blk:c", ("k5",)),  # emptied -> deleted
        place._GroupRow("g4", "user", ("k6", "k7")),  # loses k6, stays
        place._GroupRow("g5", "blk:a", ("k8",)),  # duplicate name -> deleted
        place._GroupRow("g6", "blk:d", ("k9",)),  # untouched
    ]
    blocks = [
        place.BlockArgs("a", ("R1", "R3"), None),
        place.BlockArgs("new", ("R5", "R6"), None),
    ]
    kiid_of = {"R1": "k1", "R3": "k3", "R5": "k5", "R6": "k6"}
    fps = frozenset({f"k{i}" for i in range(1, 10)})
    plan = place.plan_blocks(rows, blocks, kiid_of, fps, replace=False)
    assert plan.joins == (("g1", ("k1", "k3", "track")),)
    assert set(plan.leaves) == {("g2", ("k4",)), ("g4", ("k7",))}
    assert plan.creates == (("blk:new", ("k5", "k6")),)
    assert set(plan.deletes) == {"g3", "g5"}
    assert {r.kiid for r in plan.before} == {"g1", "g2", "g3", "g4", "g5"}
    plan = place.plan_blocks(rows, blocks, kiid_of, fps, replace=True)
    assert set(plan.deletes) == {"g2", "g3", "g5", "g6"}
    assert plan.leaves == (("g4", ("k7",)),)


def test_unplaced_blocks_without_courtyard_uses_pads() -> None:
    """A block member without a courtyard counts as placed when its pads lie inside the
    outline (else the block would stay "unplaced" forever)."""
    inside = cap("C1", "A", "B", (50.0, 40.0), courtyard=())
    outside = cap("C2", "A", "B", (150.0, 40.0), courtyard=())
    groups = (("blk:in", ("C1",)), ("blk:out", ("C2",)))
    m = circuit(inside, outside, groups=groups)
    assert place._unplaced_blocks(m) == ["out"]


# -- R2: pcb_floorplan (read) and pcb_place_blocks (write) ---------------------------------


def _any_spec(name: str) -> Any:
    return next(s for s in TOOL_SPECS if s.name == name)


def test_round2_tools_registered() -> None:
    names = [s.name for s in TOOL_SPECS]
    assert names.count("pcb_floorplan") == 1 and names.count("pcb_place_blocks") == 1
    fp = _any_spec("pcb_floorplan")
    assert not fp.mutating and fp.prepare is None
    pb = _any_spec("pcb_place_blocks")
    assert pb.mutating and pb.changes_documents and pb.prepare is not None


@pytest.mark.parametrize("name", ["pcb_floorplan", "pcb_place_blocks"])
def test_round2_schemas_bounded(name: str) -> None:
    schema = _any_spec(name).input_schema
    assert unbounded(schema) == []
    for path, o in _objects(schema):
        assert o.get("additionalProperties") is False, path
    props = schema["properties"]
    assert (props["blocks"]["minItems"], props["blocks"]["maxItems"]) == (1, 32)
    assert (props["keep_channels"]["minimum"], props["keep_channels"]["maximum"]) == (0, 5)
    assert schema["required"] == []
    assert ("unroute" in props) is (name == "pcb_place_blocks")


@pytest.mark.parametrize(
    ("args", "write"),
    [
        ({"blocks": []}, True),
        ({"blocks": ["a", "a"]}, True),
        ({"blocks": ["bad\nname"]}, False),
        ({"keep_channels": 6}, False),
        ({"keep_channels": -1}, True),
        ({"unroute": True}, False),  # read tool: no unroute
        ({"unroute": "yes"}, True),
        ({"extra": 1}, True),
    ],
)
def test_floorplan_args_refused(args: dict[str, Any], write: bool) -> None:
    with pytest.raises(StructuredToolError) as err:
        place._parse_floorplan_args(args, write=write)
    assert err.value.code == "invalid_argument"


def test_floorplan_args_ok() -> None:
    assert place._parse_floorplan_args({}, write=False) == (
        None,
        place.DEFAULT_KEEP_CHANNELS_MM,
        False,
    )
    got = place._parse_floorplan_args(
        {"blocks": ["mcu", "pwr"], "keep_channels": 1.5, "unroute": True}, write=True
    )
    assert got == (["mcu", "pwr"], 1.5, True)


def test_proposals_need_outline_blocks_and_known_names() -> None:
    from dataclasses import replace

    groups = (("blk:in", ("C1",)), ("blk:b2", ("C2",)))
    m = circuit(cap("C1", "A", "B"), cap("C2", "A", "B", (60.0, 40.0)), groups=groups)
    got = place._proposals(m, None)
    assert [(b.name, b.refs) for b in got] == [("b2", ("C2",)), ("in", ("C1",))]
    with pytest.raises(StructuredToolError) as err:
        place._proposals(m, ["inn"])
    assert err.value.code == "unknown_block"
    assert err.value.details == {"closest": ["in"]}
    with pytest.raises(StructuredToolError) as err:
        place._proposals(replace(m, groups=()), None)
    assert err.value.code == "invalid_argument"
    with pytest.raises(StructuredToolError) as err:
        place._proposals(replace(m, outline=replace(m.outline, kind="none")), None)
    assert err.value.code == "invalid_argument"


def test_does_not_fit_details_courtyard_fill() -> None:
    err = place._does_not_fit(
        place.DoesNotFit(900.0, 800.0, ("U1",), courtyard_fill=0.62), "The blocks"
    )
    assert err.code == "does_not_fit"
    assert err.details is not None and err.details["courtyard_fill"] == 0.62
    assert "62%" in err.why
    assert err.details["reason"] == "area"
    assert err.what == "The blocks does not fit"
    shape = place._does_not_fit(
        place.DoesNotFit(840.0, 1400.0, ("U1",), courtyard_fill=0.6), "The blocks", plural=True
    )
    assert shape.what == "The blocks do not fit"
    assert shape.details is not None and shape.details["reason"] == "shape"
    assert "do not tile" in shape.why and "840" not in shape.why
    plain = place._does_not_fit(place.DoesNotFit(1.0, 0.5, ("U1",)), "Block 'a'")
    assert plain.details is not None and "courtyard_fill" not in plain.details
