"""sch_list_items helpers (#3): bbox clipping, text matching, argument checks, order."""

from __future__ import annotations

from typing import Any

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.edit.geometry import Box
from copperchat.kicad_tools.edit.list_items import (
    MAX_LIMIT,
    TYPES,
    _order,
    _parse,
    _segment_touches,
    _text_matcher,
)

BOX = Box(10.0, 10.0, 20.0, 20.0)


@pytest.mark.parametrize(
    ("a", "b", "hit"),
    [
        ((0, 15), (30, 15), True),  # crosses the box
        ((12, 12), (18, 18), True),  # inside
        ((0, 10), (30, 10), True),  # runs along the top edge
        ((20, 0), (20, 30), True),  # runs along the right edge
        ((5, 5), (10, 10), True),  # ends on a corner
        ((0, 0), (30, 5), False),  # passes above
        ((21, 0), (21, 30), False),  # just right of it
        ((0, 0), (9, 9), False),  # stops short of the corner
        ((15, 15), (15, 15), True),  # a point inside
        ((25, 25), (25, 25), False),  # a point outside
    ],
)
def test_segment_touches(a: Any, b: Any, hit: bool) -> None:
    assert _segment_touches(BOX, a, b) is hit
    assert _segment_touches(BOX, b, a) is hit


def test_text_matcher_substring_and_glob() -> None:
    assert _text_matcher(None) is None and _text_matcher("") is None
    sub = _text_matcher("vin")
    assert sub is not None
    assert sub(["Power.VIN"]) and sub(["x", "VIN_EXT"]) and not sub(["GND"])
    glob = _text_matcher("power.v*")
    assert glob is not None
    assert glob(["Power.VIN"]) and not glob(["VIN"]) and not glob(["xPower.VIN"])
    one = _text_matcher("R?")
    assert one is not None
    assert one(["R1"]) and not one(["R10"])
    cls = _text_matcher("[ab]1")
    assert cls is not None
    assert cls(["A1"]) and not cls(["C1"])


def test_parse_defaults() -> None:
    a = _parse({})
    assert a["types"] == set(TYPES)
    assert (a["bbox"], a["limit"], a["offset"]) == (None, 100, 0)
    assert (a["sheet"], a["net"], a["text"]) == (None, None, None)


def test_parse_bbox_normalised() -> None:
    box = _parse({"bbox": [20, 30, 10, 5]})["bbox"]
    assert (box.x0, box.y0, box.x1, box.y1) == (10, 5, 20, 30)


@pytest.mark.parametrize(
    "args",
    [
        {"types": []},
        {"types": "label"},
        {"types": ["label", "bogus"]},
        {"bbox": [0, 0, 1]},
        {"bbox": [0, 0, 1, "x"]},
        {"bbox": [0, 0, 1, float("nan")]},
        {"bbox": [0, 0, 1, True]},
        {"limit": 0},
        {"limit": MAX_LIMIT + 1},
        {"limit": 2.5},
        {"limit": True},
        {"offset": -1},
        {"offset": "3"},
        {"sheet": 3},
        {"net": ["GND"]},
        {"text": 1},
    ],
)
def test_parse_refuses(args: dict[str, Any]) -> None:
    with pytest.raises(StructuredToolError) as exc:
        _parse(args)
    assert exc.value.code == "invalid_argument"


def test_parse_limits_accepted() -> None:
    assert _parse({"limit": MAX_LIMIT, "offset": 7})["limit"] == MAX_LIMIT
    assert _parse({"types": ["wire", "label"]})["types"] == {"wire", "label"}


def test_order_type_then_y_x_uuid() -> None:
    items = [
        {"type": "wire", "uuid": "b", "start_mm": [0, 5]},
        {"type": "label", "uuid": "z", "position_mm": [9, 1]},
        {"type": "label", "uuid": "a", "position_mm": [1, 1]},
        {"type": "label", "uuid": "c", "position_mm": [1, 1]},
        {"type": "label", "uuid": "d", "position_mm": [0, 2]},
        {"type": "sheet", "uuid": "e", "position_mm": [0, 0]},
    ]
    assert [i["uuid"] for i in sorted(items, key=_order)] == ["a", "c", "z", "d", "b", "e"]
