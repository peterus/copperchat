"""The shared sheet-edge rule (#9) on plain geometry, and the router honouring it."""

from __future__ import annotations

import itertools

import pytest

from copperchat.kicad_tools.edit.geometry import Box, PinEnd, Point
from copperchat.kicad_tools.edit.router import Obstacles, route
from copperchat.kicad_tools.edit.scene import Scene, SheetPinInfo, WireInfo
from copperchat.kicad_tools.edit.sheet_edges import (
    SHEET_EDGE_CLEARANCE_MM,
    SheetBox,
    edge_warnings,
    sheet_edge_problems,
)
from tests.unit.edit.test_router import SHEET, World

BOX = Box(50.8, 50.8, 101.6, 88.9)
SHEETS = [SheetBox("s1", "Power", BOX)]
PIN: Point = (50.8, 53.34)  # VIN on the left edge


def _problems(a: Point, b: Point) -> list[str]:
    return sheet_edge_problems((a, b), SHEETS, sheet_pins=[PIN])


@pytest.mark.parametrize(
    ("a", "b", "fragment"),
    [
        ((55.88, 50.8), (66.04, 50.8), "runs on the top edge"),
        ((55.88, 49.53), (66.04, 49.53), "1.27 mm from the top edge"),
        ((49.53, 60.96), (49.53, 71.12), "1.27 mm from the left edge"),
        ((102.87, 60.96), (102.87, 71.12), "1.27 mm from the right edge"),
        ((55.88, 90.17), (66.04, 90.17), "1.27 mm from the bottom edge"),
        ((60.96, 40.64), (60.96, 50.8), "touches the top edge"),
        ((40.64, 60.96), (50.8, 60.96), "touches the left edge"),
        ((40.64, 50.8), (50.8, 50.8), "touches the left edge"),  # onto the corner
        ((60.96, 40.64), (60.96, 60.96), "crosses sheet Power"),
        ((60.96, 53.34), (50.8, 53.34), "crosses sheet Power"),  # onto the pin from inside
        ((40.64, 53.34), (60.96, 53.34), "crosses sheet Power"),  # on through the pin
        ((50.8, 92.71), (50.8, 53.34), "runs on the left edge"),
    ],
)
def test_breaking_segments(a: Point, b: Point, fragment: str) -> None:
    problems = _problems(a, b)
    assert any(fragment in p for p in problems), problems
    assert any(fragment in p for p in _problems(b, a))  # the direction does not matter


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ((55.88, 48.26), (66.04, 48.26)),  # parallel at exactly 2.54
        ((40.64, 53.34), (50.8, 53.34)),  # perpendicular onto the sheet pin
        ((50.8, 53.34), (40.64, 53.34)),
        ((60.96, 40.64), (60.96, 49.53)),  # perpendicular, stops short of the edge
        ((40.64, 49.53), (49.53, 49.53)),  # diagonal neighbour of the corner, no overlap
        ((30.48, 30.48), (40.64, 30.48)),  # far away
    ],
)
def test_clear_segments(a: Point, b: Point) -> None:
    assert _problems(a, b) == []


def test_clearance_is_2_54() -> None:
    assert SHEET_EDGE_CLEARANCE_MM == 2.54


def _sheet_world() -> tuple[World, PinEnd, PinEnd]:
    """VIN on the left edge of the sheet; R2.1 right below the edge line, pointing up."""
    w = World()
    w.scene.sheets["s1"] = BOX
    vin = PinEnd(("/root", "s1", "VIN"), "Power", "VIN", "VIN", "sheet_pin", PIN, "left")
    w.scene.sheet_pins["p1"] = SheetPinInfo("p1", "s1", "/root/s1", vin)
    r2 = w.part(
        "r2", "R2", Box(49.53, 92.71, 52.07, 100.33),
        [("1", (50.8, 92.71), "up"), ("2", (50.8, 100.33), "down")],
    )  # fmt: skip
    return w, r2[0], vin


def test_route_to_a_sheet_pin_keeps_the_clearance() -> None:
    """The sheet pin's key names the sheet symbol: that must not exempt the route's last
    segment from the sheet (it used to run straight up along the edge)."""
    w, r2, vin = _sheet_world()
    r = route(r2, vin, w.scene, w.netlist, allowed_nets=w.allowed(r2), item_nets=w.item_nets)
    assert r is not None
    for a, b in itertools.pairwise(r.points):
        assert _problems(a, b) == [], r.points
    assert r.points[-1] == PIN and r.points[-2][1] == PIN[1]  # perpendicular arrival


def test_obstacles_report_the_rule() -> None:
    w, r2, _vin = _sheet_world()
    obstacles = Obstacles(w.scene, w.netlist, w.allowed(r2), w.item_nets)
    problems = obstacles.problems([(50.8, 92.71), PIN], ("s1", "s1"))
    assert any("runs on the left edge of sheet Power" in p for p in problems), problems


def test_edge_warnings_name_touched_existing_wires_only() -> None:
    scene = Scene(sheet=SHEET, sheets={"s1": BOX})
    scene.wires["old"] = WireInfo("old", (55.88, 49.53), (66.04, 49.53))
    scene.wires["far"] = WireInfo("far", (80.0, 49.53), (90.0, 49.53))
    out = edge_warnings(scene, [(60.96, 49.53), (60.96, 40.64)])
    assert len(out) == 1 and "old" in out[0] and "1.27 mm" in out[0], out
    assert edge_warnings(scene, [(60.96, 30.48), (60.96, 40.64)]) == []
