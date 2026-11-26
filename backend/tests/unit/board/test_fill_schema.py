"""The `pcb_route_fill` schema and argument parsing (SP4 P6b Task 1): registered,
two-phase, bounded, `additionalProperties: false`; out-of-range values refuse with
`invalid_argument` before the board is touched."""

from __future__ import annotations

from typing import Any

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.board import fill
from copperchat.kicad_tools.board.specs import TOOL_SPECS
from copperchat.kicad_tools.board.specs_fill import FILL_SPECS

from .test_place_schemas import _objects
from .test_schema_bounds import unbounded


def _spec() -> Any:
    (s,) = FILL_SPECS
    return s


def test_registered_two_phase() -> None:
    assert [s.name for s in TOOL_SPECS].count("pcb_route_fill") == 1
    s = _spec()
    assert s.name == "pcb_route_fill"
    assert s.mutating and s.changes_documents
    assert s.prepare is not None and s.apply is not None


def test_bounded_and_closed() -> None:
    schema = _spec().input_schema
    assert unbounded(schema) == []
    for path, o in _objects(schema):
        assert o.get("additionalProperties") is False, path
    props = schema["properties"]
    assert (props["timeout_s"]["minimum"], props["timeout_s"]["maximum"]) == (30, 600)
    assert (props["passes"]["minimum"], props["passes"]["maximum"]) == (1, 100)
    assert props["relock_nets"]["maxItems"] == 50
    assert props["relock_nets"]["items"]["maxLength"] == 256
    assert props["plane_layers"]["maxItems"] == 4
    assert schema["required"] == []


def test_defaults() -> None:
    a = fill.parse_fill_args({})
    assert (a.timeout_s, a.passes, a.relock_nets, a.plane_layers) == (300, 30, (), None)
    a = fill.parse_fill_args({"relock_nets": ["A", "A", "B"], "plane_layers": []})
    assert a.relock_nets == ("A", "B")
    assert a.plane_layers == ()


@pytest.mark.parametrize(
    "args",
    [
        {"timeout_s": 601},
        {"timeout_s": 29},
        {"timeout_s": 30.5},
        {"timeout_s": True},
        {"timeout_s": float("nan")},
        {"passes": 0},
        {"passes": 101},
        {"relock_nets": [f"N{i}" for i in range(51)]},
        {"relock_nets": ["x" * 257]},
        {"plane_layers": ["In1.Cu"] * 1 + ["In2.Cu", "In3.Cu", "In4.Cu", "In5.Cu"]},
        {"extra": 1},
        [],
    ],
)
def test_rejected(args: Any) -> None:
    with pytest.raises(StructuredToolError) as err:
        fill.parse_fill_args(args)
    assert err.value.code == "invalid_argument"


def test_worst_case_constants() -> None:
    assert fill.FILL_FIXED_S == 405
    assert fill.FILL_WORST_CASE_S == 1005
    assert fill.IO_BUDGET_S == 60
    assert fill.POST_DEADLINE_S == 120
