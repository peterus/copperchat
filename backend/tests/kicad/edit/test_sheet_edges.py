"""One sheet-edge rule for sch_add_wire, sch_reroute and sch_connect (#9, spec §8): no
wire segment on a sheet symbol's edge or parallel to it within 2.54 mm; a wire may only
touch the edge perpendicularly at a sheet pin. Existing violating wires: warn, not refuse.
Real KiCad; the sheet 'Power' (pin VIN on its left edge) made through the tools."""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from copperchat.db import Database
from copperchat.kicad_session import KicadSession
from copperchat.kicad_tools.edit.geometry import Box
from tests.kicad.edit.connect_support import place, scene, state
from tests.kicad.edit.helpers import wire
from tests.kicad.edit.test_sheet_pin_connect import _power_sheet
from tests.support.edit import call_tool, edit_ctx

pytestmark = pytest.mark.kicad

Pt = tuple[float, float]
CLEARANCE = 2.54


def _close_to_edge(a: Pt, b: Pt, box: Box, pin: Pt | None) -> list[str]:
    """Independent oracle: why segment a-b breaks the rule for `box` (empty = fine)."""
    eps = 0.01
    out = []
    horizontal = abs(a[1] - b[1]) < eps
    lo, hi = sorted((a[0], b[0]) if horizontal else (a[1], b[1]))
    fixed = a[1] if horizontal else a[0]
    edges = (box.y0, box.y1) if horizontal else (box.x0, box.x1)
    span = (box.x0, box.x1) if horizontal else (box.y0, box.y1)
    for e in edges:  # parallel edges: distance below the clearance with overlapping extent
        if abs(fixed - e) < CLEARANCE - eps and min(hi, span[1]) - max(lo, span[0]) > eps:
            out.append(f"parallel at {abs(fixed - e):g} mm")
    # perpendicular: touching an edge only at a sheet pin end
    cross = (box.x0, box.x1) if horizontal else (box.y0, box.y1)
    cspan = (box.y0, box.y1) if horizontal else (box.x0, box.x1)
    if cspan[0] - eps <= fixed <= cspan[1] + eps:
        for e in cross:
            if lo - eps <= e <= hi + eps:
                at = (e, fixed) if horizontal else (fixed, e)
                end = any(abs(at[0] - p[0]) < eps and abs(at[1] - p[1]) < eps for p in (a, b))
                at_pin = pin is not None and abs(at[0] - pin[0]) < eps and abs(at[1] - pin[1]) < eps
                if not (end and at_pin):
                    out.append(f"touches the edge at {at}")
    return out


def _violations(points: list[Pt], box: Box, pin: Pt | None) -> list[str]:
    return [m for a, b in itertools.pairwise(points) for m in _close_to_edge(a, b, box, pin)]


async def _setup(s: KicadSession, ctx: Any) -> dict[str, Any]:
    g = await _power_sheet(s, ctx)
    sc = await scene(s)
    (box,) = sc.sheets.values()
    g["box"] = box
    return g


def _add_wire_points(name: str, g: dict[str, Any]) -> list[list[float]]:
    box: Box = g["box"]
    x0, y0 = box.x0, box.y0
    return {
        "on_edge": [[x0 + 5.08, y0], [x0 + 15.24, y0]],
        "parallel_1.27": [[x0 + 5.08, y0 - 1.27], [x0 + 15.24, y0 - 1.27]],
        "parallel_2.54": [[x0 + 5.08, y0 - 2.54], [x0 + 15.24, y0 - 2.54]],
        "perpendicular_pin": [g["r1_at"], g["pin_at"]],
        "perpendicular_nopin": [[x0 + 10.16, y0 - 10.16], [x0 + 10.16, y0]],
    }[name]


def _reroute_points(name: str, g: dict[str, Any]) -> list[list[float]]:
    """A detour from R1's pin to VIN whose last vertical runs `d` mm left of the edge."""
    (rx, ry), (px, py) = g["r1_at"], g["pin_at"]
    d = {"on_edge": 0.0, "parallel_1.27": 1.27, "parallel_2.54": 2.54, "perpendicular_pin": 5.08}[
        name
    ]
    pts = [[rx, ry], [rx + 2.54, ry], [rx + 2.54, py + 5.08], [px - d, py + 5.08], [px - d, py]]
    return pts + ([[px, py]] if d else [])


ACCEPTED = {"parallel_2.54", "perpendicular_pin"}
ADD_WIRE = ("on_edge", "parallel_1.27", "parallel_2.54", "perpendicular_pin", "perpendicular_nopin")
# a two-ended reroute between R1 and VIN cannot touch the edge without running along it
REROUTE = ADD_WIRE[:4]
CASES = [*[("sch_add_wire", n) for n in ADD_WIRE], *[("sch_reroute", n) for n in REROUTE]]


@pytest.mark.parametrize(("tool", "geometry"), CASES)
async def test_sheet_edge_rule_same_for_all_tools(
    blank_session: KicadSession, edit_db: Database, tool: str, geometry: str
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    g = await _setup(s, ctx)
    if tool == "sch_add_wire":
        pts = _add_wire_points(geometry, g)
        args: dict[str, Any] = {"points": pts}
    else:
        out, err = await call_tool(ctx, "sch_add_wire", points=[g["r1_at"], g["pin_at"]])
        assert not err, out
        pts = _reroute_points(geometry, g)
        args = {"wires": [c["uuid"] for c in out["created"] if c["kind"] == "wire"], "points": pts}
    oracle = _violations([tuple(p) for p in pts], g["box"], tuple(g["pin_at"]))  # type: ignore[misc]
    assert bool(oracle) == (geometry not in ACCEPTED), oracle  # the table itself is right
    before = await state(s)
    out, err = await call_tool(ctx, tool, **args)
    if geometry in ACCEPTED:
        assert not err, out
    else:
        assert err, out
        assert out["error"]["code"] == "overlap", out
        assert "sheet" in out["error"]["why"], out
        assert await state(s) == before


async def test_connect_keeps_sheet_edge_clearance(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """R2 stands right below VIN's edge (its pin 1 on the edge line, pointing up): the
    straight route up along the edge is the cheapest, and must not be taken."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    g = await _setup(s, ctx)
    box: Box = g["box"]
    await place(s, ("Device:R", "R2", (box.x0, box.y1 + 7.62), 0))
    r2, err = await call_tool(ctx, "sch_get_pin", pin="R2.1")
    assert not err and r2["at_mm"] == pytest.approx([box.x0, box.y1 + 3.81]), r2
    out, err = await call_tool(ctx, "sch_connect", **{"from": "R2.1", "to": "Power.VIN"})
    assert not err, out
    pts = [tuple(p) for p in out["route"]["points"]]
    assert not _violations(pts, box, tuple(g["pin_at"])), pts
    wires = [c["uuid"] for c in out["created"] if c["kind"] == "wire"]
    # what sch_connect drew passes sch_reroute's check unchanged
    again, err = await call_tool(ctx, "sch_reroute", wires=wires, points=[list(p) for p in pts])
    assert not err, again


async def test_existing_violating_wire_warns_not_refuses(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """A legacy wire 1.27 mm above the sheet's top edge (drawn before the rule): a new wire
    T-joining it is accepted (it keeps the rule itself) and the result names the old one."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    g = await _setup(s, ctx)
    box: Box = g["box"]
    y = box.y0 - 1.27
    legacy = wire((box.x0 + 5.08, y), (box.x0 + 15.24, y))
    async with s.commit("legacy wire"):
        await s.call(lambda h: h.schematic().create_items([legacy]))
    out, err = await call_tool(
        ctx, "sch_add_wire", points=[[box.x0 + 10.16, y], [box.x0 + 10.16, y - 10.16]]
    )
    assert not err, out
    (warning,) = out["warnings"]
    assert legacy.proto.id.value in warning and "1.27 mm from the top edge" in warning, warning
    # a wire that does not touch the legacy one carries no warning
    out, err = await call_tool(
        ctx, "sch_add_wire", points=[[box.x0 + 30.48, y - 5.08], [box.x0 + 40.64, y - 5.08]]
    )
    assert not err, out
    assert "warnings" not in out
