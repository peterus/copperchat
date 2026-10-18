"""Orthogonal router (E5 Task 1) on scenes built from dataclasses, without KiCad."""

from __future__ import annotations

import itertools

from copperchat.kicad_tools.access import Netlist, PinKey
from copperchat.kicad_tools.edit.changeset import SheetRef
from copperchat.kicad_tools.edit.geometry import Box, PinEnd, Point, on_grid, on_segment
from copperchat.kicad_tools.edit.router import (
    WireTarget,
    needed_junctions,
    route,
    route_problems,
)
from copperchat.kicad_tools.edit.scene import Scene, SymbolInfo, WireInfo

SHEET = SheetRef(path="/root", page="1", file="t.kicad_sch", name="", human_path="/")


def _pin(uuid: str, ref: str, number: str, at: tuple[float, float], direction: str) -> PinEnd:
    return PinEnd(
        key=(SHEET.path, uuid, number),
        ref=ref,
        number=number,
        name="",
        type="passive",
        at=at,
        direction=direction,  # type: ignore[arg-type]
    )


def _sym(uuid: str, ref: str, body: Box, pins: list[PinEnd]) -> SymbolInfo:
    return SymbolInfo(
        uuid=uuid,
        ref=ref,
        lib_id="Device:R",
        value="",
        unit=1,
        sheet=SHEET,
        at=(body.x0, body.y0),
        rotation=0,
        mirror="",
        pins=pins,
        body=body,
        fields=[],
        power=False,
    )


class World:
    """A scene plus its netlist: every pin on its own net unless joined."""

    def __init__(self) -> None:
        self.scene = Scene(sheet=SHEET)
        self.nets: dict[str, set[PinKey]] = {}
        self.item_nets: dict[str, str] = {}

    def part(
        self, uuid: str, ref: str, body: Box, pins: list[tuple[str, tuple[float, float], str]]
    ) -> list[PinEnd]:
        ends = [_pin(uuid, ref, n, at, d) for n, at, d in pins]
        self.scene.symbols[uuid] = _sym(uuid, ref, body, ends)
        for p in ends:
            self.nets[f"unconnected-({ref}-Pad{p.number})"] = {p.key}
        return ends

    def wire(self, uuid: str, a: tuple[float, float], b: tuple[float, float], net: str) -> None:
        self.scene.wires[uuid] = WireInfo(uuid, a, b)
        self.item_nets[uuid] = net

    def join(self, net: str, *pins: PinEnd) -> None:
        for p in pins:
            for keys in self.nets.values():
                keys.discard(p.key)
        self.nets.setdefault(net, set()).update(p.key for p in pins)

    @property
    def netlist(self) -> Netlist:
        return Netlist({n: frozenset(k) for n, k in self.nets.items() if k})

    def allowed(self, *pins: PinEnd, extra: tuple[str, ...] = ()) -> set[str]:
        nl = self.netlist
        return {n for p in pins if (n := nl.net_of(p.key)) is not None} | set(extra)


def _resistor_h(w: World, uuid: str, ref: str, x: float, y: float) -> list[PinEnd]:
    """A horizontal 2-pin part: pin 1 end at (x, y) pointing left, pin 2 at x + 7.62."""
    return w.part(
        uuid, ref, Box(x, y - 1.27, x + 7.62, y + 1.27),
        [("1", (x, y), "left"), ("2", (x + 7.62, y), "right")],
    )  # fmt: skip


def _segments(points: list[Point]) -> list[tuple[Point, Point]]:
    return list(itertools.pairwise(points))


def _assert_clean(points: list[tuple[float, float]]) -> None:
    assert all(on_grid(p) for p in points)
    for a, b in _segments(points):
        assert abs(a[0] - b[0]) < 1e-6 or abs(a[1] - b[1]) < 1e-6


def test_straight_and_l_routes() -> None:
    w = World()
    a = _resistor_h(w, "ua", "R1", 2.54, 10.16)[1]  # pin 2 at (10.16, 10.16), right
    b = _resistor_h(w, "ub", "R2", 30.48, 10.16)[0]  # pin 1 at (30.48, 10.16), left
    r = route(a, b, w.scene, w.netlist, allowed_nets=w.allowed(a, b), item_nets=w.item_nets)
    assert r is not None
    assert r.points == [(10.16, 10.16), (30.48, 10.16)]
    assert r.junction_at is None

    w = World()
    a = _resistor_h(w, "ua", "R1", 2.54, 10.16)[1]
    c = w.part("uc", "R3", Box(29.21, 25.4, 31.75, 33.02), [("1", (30.48, 25.4), "up")])[0]
    r = route(a, c, w.scene, w.netlist, allowed_nets=w.allowed(a, c), item_nets=w.item_nets)
    assert r is not None
    assert r.points == [(10.16, 10.16), (30.48, 10.16), (30.48, 25.4)]


def test_avoids_symbol_bodies() -> None:
    w = World()
    a = _resistor_h(w, "ua", "R1", 2.54, 10.16)[1]
    b = _resistor_h(w, "ub", "R2", 30.48, 10.16)[0]
    blocker = Box(17.78, 6.35, 22.86, 13.97)
    w.part("ux", "U9", blocker, [])
    r = route(a, b, w.scene, w.netlist, allowed_nets=w.allowed(a, b), item_nets=w.item_nets)
    assert r is not None
    _assert_clean(r.points)
    assert len(r.points) - 1 <= 3
    assert not any(blocker.hit_by_segment(p, q) for p, q in _segments(r.points))
    allowed = w.allowed(a, b)
    assert route_problems(r.points, w.scene, w.netlist, allowed, item_nets=w.item_nets) == []


def test_never_crosses_foreign_pin_end() -> None:
    w = World()
    a = _resistor_h(w, "ua", "R1", 2.54, 10.16)[1]
    b = _resistor_h(w, "ub", "R2", 30.48, 10.16)[0]
    # A lone foreign pin end right on the straight line (its body far below).
    foreign = w.part("uf", "J1", Box(19.05, 30.0, 21.59, 35.0), [("1", (20.32, 10.16), "up")])[0]
    allowed = w.allowed(a, b)
    straight = [a.at, b.at]
    problems = route_problems(straight, w.scene, w.netlist, allowed, item_nets=w.item_nets)
    assert any("J1.1" in p for p in problems)
    r = route(a, b, w.scene, w.netlist, allowed_nets=allowed, item_nets=w.item_nets)
    assert r is not None
    assert not any(on_segment(foreign.at, p, q) for p, q in _segments(r.points))


def test_t_join_onto_target_net_wire() -> None:
    w = World()
    a = _resistor_h(w, "ua", "R1", 2.54, 15.24)[1]  # (10.16, 15.24), right
    w.wire("w1", (20.32, 2.54), (20.32, 30.48), "VIN")
    target = WireTarget("w1", (20.32, 2.54), (20.32, 30.48), "VIN")
    r = route(
        a, target, w.scene, w.netlist, allowed_nets=w.allowed(a, extra=("VIN",)),
        item_nets=w.item_nets,
    )  # fmt: skip
    assert r is not None
    assert r.points == [(10.16, 15.24), (20.32, 15.24)]
    assert r.junction_at == (20.32, 15.24)
    assert needed_junctions(r.points, w.scene) == [(20.32, 15.24)]


def test_t_join_never_lands_on_a_foreign_crossing() -> None:
    w = World()
    a = _resistor_h(w, "ua", "R1", 2.54, 15.24)[1]
    w.wire("w1", (20.32, 2.54), (20.32, 30.48), "VIN")
    w.wire("w2", (15.24, 15.24), (25.4, 15.24), "OTHER")  # crosses w1 at (20.32, 15.24)
    target = WireTarget("w1", (20.32, 2.54), (20.32, 30.48), "VIN")
    r = route(
        a, target, w.scene, w.netlist, allowed_nets=w.allowed(a, extra=("VIN",)),
        item_nets=w.item_nets,
    )  # fmt: skip
    assert r is not None
    assert r.points[-1] != (20.32, 15.24)
    for p, q in _segments(r.points):
        assert not on_segment((15.24, 15.24), p, q)  # the foreign wire end


def test_items_of_joined_nets_are_contacts_not_obstacles() -> None:
    w = World()
    a = _resistor_h(w, "ua", "R1", 2.54, 10.16)[1]
    b = _resistor_h(w, "ub", "R2", 30.48, 10.16)[0]
    w.join("SIG", b)
    # A wire of b's net ends on the straight line: a contact (same net), not an obstacle.
    w.wire("w1", (20.32, 10.16), (20.32, 20.32), "SIG")
    allowed = w.allowed(a, b)
    r = route(a, b, w.scene, w.netlist, allowed_nets=allowed, item_nets=w.item_nets)
    assert r is not None and r.points == [a.at, b.at]
    assert (20.32, 10.16) in needed_junctions(r.points, w.scene)
    # The same wire on a foreign net blocks the straight route.
    w.item_nets["w1"] = "OTHER"
    r = route(a, b, w.scene, w.netlist, allowed_nets=allowed, item_nets=w.item_nets)
    assert r is not None and r.points != [a.at, b.at]
    assert not any(on_segment((20.32, 10.16), p, q) for p, q in _segments(r.points))


def test_perpendicular_stub_when_corridor_blocked() -> None:
    w = World()
    a = _resistor_h(w, "ua", "R1", 2.54, 10.16)[1]  # (10.16, 10.16) pointing right
    b = w.part("ub", "R2", Box(30.48, 19.05, 38.1, 21.59), [("1", (30.48, 20.32), "left")])[0]
    # A body right in front of a: nothing may leave a to the right.
    w.part("ux", "U9", Box(10.8, 5.08, 16.51, 15.24), [])
    r = route(a, b, w.scene, w.netlist, allowed_nets=w.allowed(a, b), item_nets=w.item_nets)
    assert r is not None
    _assert_clean(r.points)
    first = (r.points[1][0] - a.at[0], r.points[1][1] - a.at[1])
    assert first[0] == 0 and first[1] != 0  # leaves a perpendicular to its direction


def test_none_beyond_three_segments() -> None:
    w = World()
    a = _resistor_h(w, "ua", "R1", 2.54, 10.16)[1]  # (10.16, 10.16) pointing right
    # b's pin points right, its body lies between a and b: entering b needs a U-turn.
    b = w.part("ub", "R2", Box(22.86, 6.35, 30.48, 13.97), [("1", (30.48, 10.16), "right")])[0]
    for x in (10.16, 30.48):  # foreign pins above/below both pin ends: no sideways exits
        for y in (7.62, 12.7):
            w.part(f"f{x}{y}", f"J{len(w.scene.symbols)}", Box(x + 50, y, x + 51, y + 1),
                   [("1", (x, y), "up")])  # fmt: skip
    allowed = w.allowed(a, b)
    assert route(a, b, w.scene, w.netlist, allowed_nets=allowed, item_nets=w.item_nets) is None


def test_route_never_overlaps_or_corners_on_foreign_wire() -> None:
    w = World()
    a = _resistor_h(w, "ua", "R1", 2.54, 10.16)[1]
    b = _resistor_h(w, "ub", "R2", 30.48, 10.16)[0]
    w.wire("w1", (12.7, 10.16), (27.94, 10.16), "OTHER")  # along the straight line
    allowed = w.allowed(a, b)
    problems = route_problems([a.at, b.at], w.scene, w.netlist, allowed, item_nets=w.item_nets)
    assert problems
    r = route(a, b, w.scene, w.netlist, allowed_nets=allowed, item_nets=w.item_nets)
    assert r is not None
    assert route_problems(r.points, w.scene, w.netlist, allowed, item_nets=w.item_nets) == []


def test_off_grid_pin_is_not_routed() -> None:
    w = World()
    a = w.part("ua", "R1", Box(0, 9, 10.2, 11.3), [("1", (10.2, 10.16), "right")])[0]
    b = _resistor_h(w, "ub", "R2", 30.48, 10.16)[0]
    assert route(a, b, w.scene, w.netlist, allowed_nets=w.allowed(a, b)) is None
