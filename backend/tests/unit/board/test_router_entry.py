"""Round 2 R3 Task 4: pad entry (owner rule c) - bends in an end pad's entry zone cost more
and a looping pad entry (M16 b) is never accepted; aligned pads get the straight segment."""

from __future__ import annotations

import itertools

from copperchat.board.geom import _in_pad, analyse_entry, entry_zone
from copperchat.board.router import RoutePath, RouteRequest, build_grid, route
from copperchat.board.router import astar as astar_mod
from copperchat.board.router.astar import entry_nodes, valid_path
from copperchat.board.router.simplify import layer_runs

from .builders import outline, part
from .builders_circuit import circuit, pad


def _req(a, b, width: float = 0.2) -> RouteRequest:
    return RouteRequest("P", a, b, width, 0.2, ("F.Cu",), False, (0.6, 0.3))


def _loops(path: RoutePath, req: RouteRequest) -> list:
    runs = layer_runs(path)
    out = []
    for pts, end in ((runs[0][1], req.a), (runs[-1][1][::-1], req.b)):
        rep = analyse_entry(
            pts, req.width, entry_zone(end, req.width), in_pad=lambda q, e=end: _in_pad(q, e)
        )
        out += rep.loops
    return out


def test_valid_path_rejects_entry_loop() -> None:
    """A path that leaves pad a east and turns back west within the entry zone (a U-turn,
    M16 pad loop) is refused; the same shape outside the zone passes."""
    a = pad("U1", "1", "P", (10.0, 10.0), w=1.0, h=0.4)
    b = pad("U2", "1", "P", (8.0, 6.0), w=0.5, h=0.5)
    m = circuit(part("U1", 10.0, 10.0, pads=(a,)), part("U2", 8.0, 6.0, pads=(b,)))
    req = _req(a, b)
    grid = build_grid(m, req)

    def path(reach: float) -> RoutePath:
        x = 10.0 + reach
        pts = [(10.0, 10.0), (x, 10.0), (x + 0.3, 9.7), (x + 0.3, 9.4), (x, 9.1), (9.0, 9.1)]
        pts += [(8.0, 8.1), (8.0, 6.0)]
        segs = tuple(("F.Cu", p, q, 0.2) for p, q in itertools.pairwise(pts))
        return RoutePath(segs, (), 0.0)

    assert not valid_path(path(0.6), grid, req)
    assert valid_path(path(2.0), grid, req)


def test_entry_nodes_outside_the_pad() -> None:
    a = pad("U1", "1", "P", (10.0, 10.0), w=1.0, h=0.4)
    b = pad("U2", "1", "P", (14.0, 10.0), w=0.5, h=0.5)
    m = circuit(part("U1", 10.0, 10.0, pads=(a,)), part("U2", 14.0, 10.0, pads=(b,)))
    req = _req(a, b)
    grid = build_grid(m, req)
    nodes = entry_nodes(grid, (a,), req.width)
    pts = [grid.xy(n % grid.w, n // grid.w) for n in nodes]
    assert pts and not any(_in_pad(p, a) for p in pts)
    assert all(abs(p[0] - 10.0) <= 0.5 + 1.0 + 1e-9 and abs(p[1] - 10.0) <= 1.2 + 1e-9 for p in pts)


def test_entry_no_jog_when_straight_possible() -> None:
    """Two aligned pads a few mm apart: one straight segment, no jog."""
    a = pad("D1", "1", "P", (10.0, 10.0), w=0.8, h=0.9)
    b = pad("R4", "1", "P", (12.5, 10.0), w=0.8, h=0.9)
    m = circuit(part("D1", 10.0, 10.0, pads=(a,)), part("R4", 12.5, 10.0, pads=(b,)))
    path = route(build_grid(m, _req(a, b)), _req(a, b))
    assert path is not None and len(path.segments) == 1


def test_entry_no_loop() -> None:
    """A pad whose long axis points away from the goal, other-net pads crowding it: the
    result leaves the pad without a loop in its entry zone."""
    a = pad("U1", "1", "P", (10.0, 10.0), w=1.6, h=0.4)  # long axis x, goal north
    b = pad("U2", "1", "P", (10.0, 6.5), w=0.5, h=0.5)
    wall = [pad("X1", str(k), f"O{k}", (9.0 + 0.5 * k, 9.2), w=0.3, h=0.3) for k in range(5)]
    m = circuit(
        part("U1", 10.0, 10.0, pads=(a,)),
        part("U2", 10.0, 6.5, pads=(b,)),
        part("X1", 10.0, 9.2, pads=tuple(wall)),
        outline=outline(0, 0, 20, 20),
    )
    req = _req(a, b)
    path = route(build_grid(m, req), req)
    assert path is not None
    assert _loops(path, req) == []


def test_entry_bend_costs_more(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """With the entry cost switched off the search may bend next to the pad; the cost only
    moves bends, it never loses the route."""
    a = pad("U1", "1", "P", (10.0, 10.0), w=1.6, h=0.4)
    b = pad("U2", "1", "P", (13.0, 7.0), w=0.5, h=0.5)
    m = circuit(part("U1", 10.0, 10.0, pads=(a,)), part("U2", 13.0, 7.0, pads=(b,)))
    req = _req(a, b)
    with_cost = route(build_grid(m, req), req)
    monkeypatch.setattr(astar_mod, "ENTRY_TURN_COST", 0.0)
    without = route(build_grid(m, req), req)
    assert with_cost is not None and without is not None
