"""The `pcb_route` / `pcb_unroute` schemas (SP4 P5b Task 1): registered, two-phase, bounded,
`additionalProperties: false` at every level; what the schema cannot express (nets or
region) refuses with `invalid_argument` before the board is touched."""

from __future__ import annotations

from typing import Any

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.board import route
from copperchat.kicad_tools.board.specs import TOOL_SPECS
from copperchat.kicad_tools.board.specs_route import ROUTE_SPECS

from .test_place_schemas import _objects
from .test_schema_bounds import unbounded

NAMES = ("pcb_route", "pcb_unroute")


def _spec(name: str) -> Any:
    return next(s for s in ROUTE_SPECS if s.name == name)


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


def test_route_limits() -> None:
    schema = _spec("pcb_route").input_schema
    assert schema["required"] == ["nets"]
    props = schema["properties"]
    nets = props["nets"]
    assert (nets["minItems"], nets["maxItems"]) == (1, 50)
    assert nets["items"]["maxLength"] == 256
    assert props["layers"]["maxItems"] == 32
    assert props["width_from"]["enum"] == ["netclass"]
    assert props["lock"]["type"] == props["allow_vias"]["type"] == "boolean"


def test_unroute_limits() -> None:
    schema = _spec("pcb_unroute").input_schema
    assert schema["required"] == []
    props = schema["properties"]
    assert props["nets"]["maxItems"] == 50
    assert props["nets"]["items"]["maxLength"] == 256
    region = props["region"]
    assert region["required"] == ["x", "y", "w", "h"]
    for k in ("w", "h"):
        assert region["properties"][k]["exclusiveMinimum"] == 0


def test_descriptions_name_next_step() -> None:
    assert "pcb_route_fill" in _spec("pcb_unroute").description
    assert "next" in _spec("pcb_route").description


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"include_locked": True},
        {"unlock_only": True},
        {"nets": []},
    ],
)
def test_unroute_needs_nets_or_region(args: dict[str, Any]) -> None:
    with pytest.raises(StructuredToolError) as err:
        route._parse_unroute(args)
    assert err.value.code == "invalid_argument"


@pytest.mark.parametrize(
    "region",
    [
        {"x": 0, "y": 0, "w": 0, "h": 1},
        {"x": 0, "y": 0, "w": 1, "h": -1},
        {"x": 0, "y": 0, "w": 1},
        {"x": 0, "y": 0, "w": 1, "h": 1, "z": 2},
        {"x": float("nan"), "y": 0, "w": 1, "h": 1},
        {"x": 2000, "y": 0, "w": 1, "h": 1},
        {"x": True, "y": 0, "w": 1, "h": 1},
    ],
)
def test_unroute_region_checked(region: dict[str, Any]) -> None:
    with pytest.raises(StructuredToolError) as err:
        route._parse_unroute({"region": region})
    assert err.value.code == "invalid_argument"


def test_unroute_parse_ok() -> None:
    nets, region, inc, unlock = route._parse_unroute(
        {"nets": ["A", "A", "B"], "region": {"x": 1, "y": 2, "w": 3, "h": 4}, "unlock_only": True}
    )
    assert nets == ["A", "B"]
    assert region == route.Rect(1.0, 2.0, 3.0, 4.0)
    assert (inc, unlock) == (False, True)


@pytest.mark.parametrize(
    "args",
    [
        {"nets": []},
        {"nets": ["A"] * 51},
        {"nets": ["x" * 257]},
        {"nets": ["A"], "width_from": "rules"},
        {"nets": ["A"], "lock": "yes"},
        {"nets": ["A"], "layers": []},
        {"nets": ["A"], "bogus": 1},
    ],
)
async def test_route_args_checked_before_board(
    args: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(route, "require_board_tools", lambda ctx: None)

    def no_board(ctx: Any) -> None:
        raise AssertionError("the board was touched")

    monkeypatch.setattr(route, "require_board", no_board)
    with pytest.raises(StructuredToolError) as err:
        await route.prepare_route(object(), args)  # type: ignore[arg-type]
    assert err.value.code == "invalid_argument"
