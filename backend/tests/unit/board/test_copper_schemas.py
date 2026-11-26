"""The copper tool schemas (SP4 P7 Task 2 Step 1): `pcb_zones`, `pcb_fanout`, `pcb_stitch`,
`pcb_tidy_silk` registered as two-phase board writes, bounded, `additionalProperties:
false` at every level; out-of-bounds input refused by the schema and by the parsers
(`invalid_argument`, before the board is touched)."""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.board.fanout import parse_fanout_args
from copperchat.kicad_tools.board.specs import TOOL_SPECS
from copperchat.kicad_tools.board.specs_copper import COPPER_SPECS
from copperchat.kicad_tools.board.stitch import parse_stitch_args
from copperchat.kicad_tools.board.zones import parse_zones_args

from .test_schema_bounds import unbounded

NAMES = ("pcb_zones", "pcb_fanout", "pcb_stitch", "pcb_tidy_silk")


def _spec(name: str) -> Any:
    return next(s for s in COPPER_SPECS if s.name == name)


def _objects(schema: Any, path: str = "$") -> list[tuple[str, dict[str, Any]]]:
    if not isinstance(schema, dict):
        return []
    out = [(path, schema)] if schema.get("type") == "object" else []
    for name, sub in (schema.get("properties") or {}).items():
        out += _objects(sub, f"{path}.{name}")
    out += _objects(schema.get("items"), f"{path}[]")
    for i, sub in enumerate(schema.get("anyOf") or []):
        out += _objects(sub, f"{path}|{i}")
    return out


def _valid(name: str, args: dict[str, Any]) -> bool:
    return Draft202012Validator(_spec(name).input_schema).is_valid(args)


def _square(n: int = 4) -> list[dict[str, float]]:
    return [{"x": float(i), "y": float(i % 2)} for i in range(n)]


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


def test_zones_schema_bounds() -> None:
    ok = {
        "action": "add",
        "zones": [
            {
                "name": "P1",
                "net": "GND",
                "layers": ["B.Cu"],
                "outline": {"polygon": _square()},
                "priority": 100,
                "clearance": 0.2,
                "thermal": {"gap": 0.5, "spoke_width": 0.4},
            }
        ],
    }
    assert _valid("pcb_zones", ok)
    assert _valid("pcb_zones", {"action": "add"})
    assert _valid("pcb_zones", {"action": "add", "zones": [{"name": "P", "outline": "board"}]})

    def zone(**kw: Any) -> dict[str, Any]:
        return {"action": "add", "zones": [{**ok["zones"][0], **kw}]}  # type: ignore[index]

    assert not _valid("pcb_zones", zone(outline={"polygon": _square(257)}))
    assert not _valid("pcb_zones", zone(outline={"polygon": _square(2)}))
    assert not _valid("pcb_zones", zone(outline="all"))
    assert not _valid("pcb_zones", zone(priority=101))
    assert not _valid("pcb_zones", zone(thermal={"gap": 0, "spoke_width": 0.3}))
    assert not _valid("pcb_zones", zone(clearance=10.5))
    assert not _valid("pcb_zones", zone(extra=1))
    assert not _valid("pcb_zones", zone(name="bad/name"))
    assert not _valid("pcb_zones", {"action": "move"})
    assert not _valid(
        "pcb_zones", {"action": "add", "zones": [{"name": f"Z{i}"} for i in range(17)]}
    )


def test_fanout_and_stitch_schema_bounds() -> None:
    assert _valid(
        "pcb_fanout", {"net": "GND", "via": {"diameter": 0.6, "drill": 0.3}, "max_stub": 3}
    )
    assert not _valid("pcb_fanout", {"net": "GND", "max_stub": 5})
    assert not _valid("pcb_fanout", {"net": "GND", "max_stub": 0.05})
    assert not _valid("pcb_fanout", {"net": "GND", "via": {"diameter": 3, "drill": 0.3}})
    assert not _valid("pcb_fanout", {})
    assert _valid("pcb_stitch", {"net": "GND", "pitch": 1, "edge_ring": True})
    assert not _valid("pcb_stitch", {"net": "GND", "pitch": 0.5})
    assert not _valid("pcb_stitch", {"net": "GND", "pitch": 51})
    assert not _valid("pcb_stitch", {"net": "GND", "extra": True})
    assert _valid("pcb_tidy_silk", {})
    assert not _valid("pcb_tidy_silk", {"height": 1.0})


def _refused(fn: Any, args: Any) -> str:
    with pytest.raises(StructuredToolError) as info:
        fn(args)
    assert info.value.code == "invalid_argument"
    return info.value.why


@pytest.mark.parametrize(
    "args",
    [
        {
            "action": "add",
            "zones": [{"name": "Z", "net": "GND", "layers": ["F.Cu"], "priority": 101}],
        },
        {
            "action": "add",
            "zones": [{"name": "Z", "net": "GND", "layers": ["F.Cu"], "priority": True}],
        },
        {
            "action": "add",
            "zones": [
                {
                    "name": "Z",
                    "net": "GND",
                    "layers": ["F.Cu"],
                    "thermal": {"gap": 0, "spoke_width": 1},
                }
            ],
        },
        {
            "action": "add",
            "zones": [{"name": "Z", "net": "GND", "layers": ["F.Cu"], "clearance": float("nan")}],
        },
        {
            "action": "add",
            "zones": [
                {
                    "name": "Z",
                    "net": "GND",
                    "layers": ["F.Cu"],
                    "outline": {"polygon": _square(257)},
                }
            ],
        },
        {
            "action": "add",
            "zones": [
                {
                    "name": "Z",
                    "net": "GND",
                    "layers": ["F.Cu"],
                    "outline": {"polygon": [{"x": 1e9, "y": 0}] * 3},
                }
            ],
        },
        {"action": "add", "zones": [{"name": "Z", "layers": ["F.Cu"]}]},
        {"action": "add", "zones": [{"name": "Z", "net": "GND", "layers": []}]},
        {"action": "update", "zones": [{"name": "Z"}]},
        {"action": "delete", "zones": [{"name": "Z", "net": "GND"}]},
        {"action": "delete"},
        {"action": "add", "zones": [{"name": "Z", "net": "N", "layers": ["F.Cu"]}] * 2},
        {"action": "add", "zones": [{"name": " Z"}]},
        {"action": "add", "zones": []},
        {"action": "add", "extra": 1},
        {"action": "nope"},
        [],
    ],
)
def test_zones_parser_refuses(args: Any) -> None:
    _refused(parse_zones_args, args)


def test_zones_parser_defaults() -> None:
    action, zones = parse_zones_args({"action": "add"})
    assert action == "add" and zones is None
    _, zones = parse_zones_args(
        {"action": "add", "zones": [{"name": "Z", "net": "N", "layers": ["F.Cu"]}]}
    )
    assert zones is not None and zones[0].outline == "board" and zones[0].priority == 0
    _, zones = parse_zones_args({"action": "update", "zones": [{"name": "Z", "clearance": 0.3}]})
    assert zones is not None and zones[0].priority is None and zones[0].clearance == 0.3


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"net": ""},
        {"net": "GND", "max_stub": 5},
        {"net": "GND", "max_stub": 0.05},
        {"net": "GND", "via": {"diameter": 0.6}},
        {"net": "GND", "via": {"diameter": 0.3, "drill": 0.3}},
        {"net": "GND", "via": {"diameter": 2.5, "drill": 0.3}},
        {"net": "GND", "extra": 1},
    ],
)
def test_fanout_parser_refuses(args: Any) -> None:
    _refused(parse_fanout_args, args)


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"net": "GND", "pitch": 0.5},
        {"net": "GND", "pitch": 51},
        {"net": "GND", "pitch": "5"},
        {"net": "GND", "edge_ring": 1},
        {"net": "GND", "via": {"diameter": 0.6, "drill": 0.7}},
    ],
)
def test_stitch_parser_refuses(args: Any) -> None:
    _refused(parse_stitch_args, args)


def test_parser_defaults() -> None:
    f = parse_fanout_args({"net": "GND"})
    assert f.max_stub == 1.0 and f.via is None
    s = parse_stitch_args({"net": "GND"})
    assert s.pitch == 5.0 and s.edge_ring is False and s.via is None
