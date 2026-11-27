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


def test_solve_never_loops() -> None:
    """USB-C A6 / B6: two tall pads of one net with another net's pad between them and
    other-net copper just beyond their ends - only a U-turn inside the entry zone connects
    them on this layer. R4 (owner 2026-11-27, M16 b / c hard): `solve` finds no route rather
    than one with the loop; `entry_ok` (explicit) still allows it."""
    from copperchat.board.router import solve

    from .builders import track

    a = pad("J1", "A6", "DP", (10.0, 10.0), w=0.3, h=1.45)
    mid = pad("J1", "A7", "DN", (10.5, 10.0), w=0.3, h=1.45)
    b = pad("J1", "B6", "DP", (11.0, 10.0), w=0.3, h=1.45)
    m = circuit(
        part("J1", 10.5, 10.0, pads=(a, mid, b)),
        outline=outline(0, 0, 20, 20),
        tracks=(
            track("X", "F.Cu", (8.0, 9.85 - 0.725 - 0.9), (13.0, 9.85 - 0.725 - 0.9), 0.2),
            track("Y", "F.Cu", (8.0, 10.725 + 0.5), (13.0, 10.725 + 0.5), 0.2),
            track("Z", "F.Cu", (9.2, 7.0), (9.2, 13.0), 0.2),
            track("W", "F.Cu", (11.8, 7.0), (11.8, 13.0), 0.2),
        ),
    )
    req = RouteRequest("DP", a, b, 0.2, 0.15, ("F.Cu",), False, (0.6, 0.3))
    assert route(build_grid(m, req), req) is None
    assert solve(m, req) is None
    path = solve(m, req, entry_ok=True)
    assert path is not None
    assert _loops(path, req)


def test_solve_order(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`solve`: default window (≤ FIRST_TRY_EXPANSIONS) → doubled window (≤
    RETRY_STRICT_EXPANSIONS), both refusing entry loops / jogs - no loop-allowing pass
    (R4); `entry_ok` lifts the rule in both."""
    calls: list[tuple[int, bool, int]] = []
    real = astar_mod.route

    def spy(grid, req, *, max_expansions=2_000_000, deadline=None, entry_ok=False):  # type: ignore[no-untyped-def]
        calls.append((grid.w, entry_ok, max_expansions))
        return None

    monkeypatch.setattr(astar_mod, "route", spy)
    a = pad("U1", "1", "P", (10.0, 10.0))
    b = pad("U2", "1", "P", (12.0, 10.0))
    m = circuit(part("U1", 10.0, 10.0, pads=(a,)), part("U2", 12.0, 10.0, pads=(b,)))
    req = _req(a, b)
    assert astar_mod.solve(m, req) is None
    (w1, e1, x1), (w2, e2, x2) = calls
    assert (e1, e2) == (False, False)
    assert w2 > w1
    assert x1 == astar_mod.FIRST_TRY_EXPANSIONS and x2 == astar_mod.RETRY_STRICT_EXPANSIONS
    calls.clear()
    astar_mod.solve(m, req, entry_ok=True)
    assert [e for _w, e, _x in calls] == [True, True]
    calls.clear()

    def clean_second(grid, req, *, max_expansions=2_000_000, deadline=None, entry_ok=False):  # type: ignore[no-untyped-def]
        calls.append((grid.w, entry_ok, max_expansions))
        return (
            real(grid, req, max_expansions=max_expansions, deadline=deadline)
            if len(calls) == 2
            else None
        )

    monkeypatch.setattr(astar_mod, "route", clean_second)
    assert astar_mod.solve(m, req) is not None
    assert len(calls) == 2  # the loop-allowing passes never ran


def test_entry_zones_judged_per_pad() -> None:
    """Review round 1: two close, offset pads whose entry zones touch - the shortest legal
    path is an S (+45° in a's zone, -45° in b's zone), which is no jog (M16 judges each
    pad's zone alone). The search must find it (the union of both zones refused it and
    returned a 4.1 mm path with 6 bends)."""
    a = pad("U1", "1", "P", (10.0, 10.0), w=1.0, h=0.4)
    b = pad("U2", "1", "P", (12.6, 10.6), w=1.0, h=0.4)
    m = circuit(part("U1", 10.0, 10.0, pads=(a,)), part("U2", 12.6, 10.6, pads=(b,)))
    req = _req(a, b)
    grid = build_grid(m, req)
    path = route(grid, req)
    assert path is not None and valid_path(path, grid, req)
    assert path.length < 3.2, path.length
    assert _loops(path, req) == []


def test_zone_state_layout() -> None:
    """`_ZONE_NEXT`: from no bend, one of either sense; a second of the same sense (90°);
    the opposite sense (jog) and a third bend (135°, loop) are refused."""
    nxt = astar_mod._ZONE_NEXT
    assert nxt[0][1] == 1 and nxt[0][2] == 2
    assert nxt[1][1] == 3 and nxt[2][2] == 4
    assert nxt[1][2] == -1 and nxt[2][1] == -1
    assert all(v == -1 for z in (3, 4) for v in nxt[z][1:])
    assert astar_mod._NZ == 1 + 2 * 4
