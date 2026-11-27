"""Octilinear A* with straight-piece rules, vias and pad entry (P5a Task 2)."""

from __future__ import annotations

import itertools
import math
from typing import Any

import pytest

from copperchat.board.geom import CHAMFER_MIN, JOG_MIN, analyse_bends
from copperchat.board.model import Pad, Point
from copperchat.board.router import RoutePath, RouteRequest, build_grid, route
from copperchat.board.router import astar as astar_mod
from copperchat.board.router.astar import DIRS, pad_dirs
from copperchat.board.router.simplify import layer_runs

from .builders import board, outline, part, track


def pad(
    net: str,
    x: float,
    y: float,
    w: float = 0.5,
    h: float = 0.5,
    *,
    rot: float = 0.0,
    layer: str = "F.Cu",
    tht: bool = False,
    ref: str = "A",
) -> Pad:
    layers = frozenset({"*.Cu", "*.Mask"}) if tht else frozenset({layer, "F.Paste", "F.Mask"})
    return Pad(
        ref=ref,
        number="1",
        net=net,
        x=x,
        y=y,
        w=w,
        h=h,
        rot=rot,
        shape="rect",
        layers=layers,
        smd=not tht,
        drill=0.8 if tht else None,
    )


def rq(
    a: Pad,
    b: Pad,
    *,
    width: float = 0.2,
    clearance: float = 0.2,
    layers: tuple[str, ...] = ("F.Cu",),
    allow_vias: bool = False,
) -> RouteRequest:
    return RouteRequest(
        net="SIG",
        a=a,
        b=b,
        width=width,
        clearance=clearance,
        layers=layers,
        allow_vias=allow_vias,
        via=(0.6, 0.3),
    )


def go(req: RouteRequest, tracks=(), *, w: float = 30, h: float = 20, **kw) -> RoutePath | None:
    m = board(
        parts=(
            part("A", req.a.x, req.a.y, pads=(req.a,)),
            part("B", req.b.x, req.b.y, pads=(req.b,)),
        ),
        outline=outline(0, 0, w, h),
        tracks=tuple(tracks),
    )
    return route(build_grid(m, req), req, **kw)


def dir_of(p: Point, q: Point) -> int:
    ang = math.degrees(math.atan2(-(q[1] - p[1]), q[0] - p[0])) % 360
    k = round(ang / 45) % 8
    assert abs(ang - k * 45) < 0.01 or abs(ang - k * 45 - 360) < 0.01
    return k


def bends(path: RoutePath, width: float) -> list[tuple[int, float, int]]:
    """(index, piece length since the previous bend, sense) per bend of each layer run."""
    out = []
    for _layer, pts in layer_runs(path):
        last_at = None
        cum = 0.0
        for i in range(1, len(pts) - 1):
            cum += math.dist(pts[i - 1], pts[i])
            d1, d2 = dir_of(pts[i - 1], pts[i]), dir_of(pts[i], pts[i + 1])
            turn = (d2 - d1) % 8
            assert turn in (1, 7), f"turn of {turn * 45} deg at {pts[i]}"
            sense = 1 if turn == 1 else -1
            out.append((i, cum - (last_at or 0.0) if last_at is not None else math.inf, sense))
            last_at = cum
    return out


def assert_clean(path: RoutePath, width: float) -> None:
    runs = layer_runs(path)
    for k, (_layer, pts) in enumerate(runs):
        r = analyse_bends(pts, width, pad_ends=(k == 0, k == len(runs) - 1))
        assert not (r.non_octilinear or r.ninety or r.acute or r.off_angle), r
        assert not (r.micro_jogs or r.short_chamfers), r


def test_straight_line() -> None:
    req = rq(pad("SIG", 5, 10), pad("SIG", 15, 10, ref="B"))
    path = go(req)
    assert path is not None
    assert path.segments == (("F.Cu", (5.0, 10.0), (15.0, 10.0), 0.2),)
    assert path.length == pytest.approx(10.0)
    assert path.vias == ()


def test_diagonal() -> None:
    req = rq(pad("SIG", 5, 5), pad("SIG", 10, 10, ref="B"))
    path = go(req)
    assert path is not None
    assert len(path.segments) == 1
    assert path.length == pytest.approx(5 * math.sqrt(2))


def _corridor() -> list:
    # an L corridor: along y = 10 to x = 15, then up to y = 2 (walls 1.6 apart)
    return [
        track("W", "F.Cu", (0.0, 10.8), (15.8, 10.8), 0.2, kiid="w1"),
        track("W", "F.Cu", (0.0, 9.2), (14.2, 9.2), 0.2, kiid="w2"),
        track("W", "F.Cu", (15.8, 10.8), (15.8, 0.0), 0.2, kiid="w3"),
        track("W", "F.Cu", (14.2, 9.2), (14.2, 0.0), 0.2, kiid="w4"),
    ]


@pytest.mark.parametrize("width", [0.2, 0.4])
def test_ninety_needs_chamfer(width: float) -> None:
    a = pad("SIG", 3, 10, 1.0, 0.4)
    b = pad("SIG", 15, 2, 0.4, 1.0, ref="B")
    req = rq(a, b, width=width, clearance=0.15 if width == 0.4 else 0.2)
    path = go(req, _corridor())
    assert path is not None
    assert_clean(path, width)
    bs = bends(path, width)
    same_pairs = [p for (_, p, s), (_, _, s0) in zip(bs[1:], bs, strict=False) if s == s0]
    assert same_pairs, bs  # the corner is two same-sense 45 deg bends
    assert all(p + 1e-9 >= CHAMFER_MIN(width) for p in same_pairs)
    assert min(same_pairs) >= (0.25 if width == 0.2 else 0.4) - 1e-9


def _step_corridor() -> list:
    # a corridor whose centre steps from y = 10.0 to y = 10.3 between x = 9 and x = 11
    return [
        track("W", "F.Cu", (0.0, 9.4), (11.0, 9.4), 0.2, kiid="u1"),
        track("W", "F.Cu", (11.0, 9.7), (20.0, 9.7), 0.2, kiid="u2"),
        track("W", "F.Cu", (0.0, 10.6), (9.0, 10.6), 0.2, kiid="l1"),
        track("W", "F.Cu", (9.0, 10.9), (20.0, 10.9), 0.2, kiid="l2"),
    ]


def test_s_offset_respects_jog_min() -> None:
    width = 0.2
    req = rq(pad("SIG", 5, 10, 1.0, 0.4), pad("SIG", 15, 10.3, 1.0, 0.4, ref="B"), width=width)
    path = go(req, _step_corridor())
    assert path is not None
    assert_clean(path, width)
    bs = bends(path, width)
    opp = [p for (_, p, s), (_, _, s0) in zip(bs[1:], bs, strict=False) if s != s0]
    assert opp
    assert all(p + 1e-9 >= JOG_MIN(width) for p in opp)


def test_no_turn_over_45_at_vertex() -> None:
    req = rq(pad("SIG", 3, 10, 1.0, 0.4), pad("SIG", 15, 2, 0.4, 1.0, ref="B"))
    path = go(req, _corridor())
    assert path is not None
    for _layer, pts in layer_runs(path):
        for p, q, r in zip(pts, pts[1:], pts[2:], strict=False):
            turn = (dir_of(q, r) - dir_of(p, q)) % 8
            assert turn in (0, 1, 7)


def test_detours_obstacle() -> None:
    wall = track("W", "F.Cu", (10.0, 6.0), (10.0, 14.0), 0.2)
    req = rq(pad("SIG", 5, 10), pad("SIG", 15, 10, ref="B"))
    path = go(req, [wall])
    assert path is not None
    assert path.length > 10.5
    assert_clean(path, 0.2)
    grid = build_grid(board(parts=(), outline=outline(0, 0, 30, 20), tracks=(wall,)), req)
    assert all(grid.segment_clear(0, p, q) for _l, p, q, _w in path.segments)


def test_uses_via_when_blocked() -> None:
    wall = track("W", "F.Cu", (10.0, -1.0), (10.0, 21.0), 0.2)
    req = rq(
        pad("SIG", 5, 10),
        pad("SIG", 15, 10, 1.6, 1.6, tht=True, ref="B"),
        layers=("F.Cu", "B.Cu"),
        allow_vias=True,
    )
    path = go(req, [wall])
    assert path is not None
    assert len(path.vias) == 1
    assert {layer for layer, _p, _q, _w in path.segments} == {"F.Cu", "B.Cu"}
    assert path.vias[0][0] < 10.0
    assert_clean(path, 0.2)


def test_no_via_when_disallowed() -> None:
    wall = track("W", "F.Cu", (10.0, -1.0), (10.0, 21.0), 0.2)
    req = rq(
        pad("SIG", 5, 10),
        pad("SIG", 15, 10, 1.6, 1.6, tht=True, ref="B"),
        layers=("F.Cu", "B.Cu"),
        allow_vias=False,
    )
    assert go(req, [wall]) is None


@pytest.mark.parametrize("rot", [0.0, 90.0, 30.0])
def test_pad_exit_long_axis(rot: float) -> None:
    a = pad("SIG", 10, 12, 1.0, 0.4, rot=rot)
    req = rq(a, pad("SIG", 10, 4, ref="B"))
    path = go(req)
    assert path is not None
    first = dir_of(*path.segments[0][1:3])
    axis = round(rot / 45) % 8
    assert first in {(axis + o) % 8 for o in (-1, 0, 1, 3, 4, 5)}
    assert first in pad_dirs(a)
    # entering b (square) is free; leaving a straight up (perpendicular to the axis) is not
    if rot == 0.0:
        assert first != 2


def test_pad_dirs() -> None:
    assert pad_dirs(pad("S", 0, 0, 1.0, 1.0)) == frozenset(range(8))
    assert pad_dirs(pad("S", 0, 0, 1.0, 0.4)) == frozenset({0, 1, 7, 4, 3, 5})
    assert pad_dirs(pad("S", 0, 0, 0.4, 1.0)) == frozenset({2, 1, 3, 6, 5, 7})
    assert pad_dirs(pad("S", 0, 0, 1.0, 0.4, rot=50.0)) == frozenset({1, 0, 2, 5, 4, 6})
    # R3: an 0603-like pad (aspect < PAD_AXIS_RATIO) has no long axis
    assert pad_dirs(pad("S", 0, 0, 0.8, 0.95)) == frozenset(range(8))
    assert pad_dirs(pad("S", 0, 0, 0.6, 0.9)) == frozenset({2, 1, 3, 6, 5, 7})
    assert DIRS[1] == (1, -1)  # counter-clockwise on screen (y down)


def test_prefers_first_layer() -> None:
    req = rq(
        pad("SIG", 5, 10, 1.6, 1.6, tht=True),
        pad("SIG", 15, 10, 1.6, 1.6, tht=True, ref="B"),
        layers=("B.Cu", "F.Cu"),
        allow_vias=True,
    )
    path = go(req)
    assert path is not None
    assert {layer for layer, _p, _q, _w in path.segments} == {"B.Cu"}


def test_max_expansions_none() -> None:
    req = rq(pad("SIG", 5, 10), pad("SIG", 25, 10, ref="B"))
    assert go(req, max_expansions=100) is None
    assert go(req) is not None


def test_route_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    wall = track("W", "F.Cu", (10.0, -1.0), (10.0, 21.0), 0.2)
    req = rq(pad("SIG", 5, 10), pad("SIG", 15, 10, ref="B"))
    m = board(parts=(), outline=outline(0, 0, 30, 20), tracks=(wall,))
    grid = build_grid(m, req)

    def boom(*_a, **_k):
        raise AssertionError("route must not build a grid")

    monkeypatch.setattr(astar_mod, "build_grid", boom)
    assert route(grid, req) is None


def test_exact_pad_centres_off_grid() -> None:
    req = rq(pad("SIG", 5.013, 10.027), pad("SIG", 15.031, 12.008, ref="B"))
    path = go(req)
    assert path is not None
    assert path.segments[0][1] == (5.013, 10.027)
    assert path.segments[-1][2] == (15.031, 12.008)
    assert_clean(path, 0.2)


def test_pad_exit_axis_until_outside_the_pad() -> None:
    # a long 2.0 x 0.4 pad and a target straight below: the segment that leaves the pad
    # still runs along the axis or at 45 degrees to it, never out of the long side
    a = pad("SIG", 10, 5, 2.0, 0.4)
    path = go(rq(a, pad("SIG", 10, 11, ref="B")))
    assert path is not None
    pts = layer_runs(path)[0][1]
    from copperchat.board.geom import _in_pad

    crossing = next((p, q) for p, q in itertools.pairwise(pts) if not _in_pad(q, a))
    assert dir_of(*crossing) in pad_dirs(a)


def test_same_node_pads_no_loop() -> None:
    req = rq(pad("SIG", 10, 10), pad("SIG", 10.02, 10.0, ref="B"))
    path = go(req)
    assert path is None or path.length <= 0.1


def test_layers_validated() -> None:
    m = board(parts=(), outline=outline(0, 0, 30, 20))
    with pytest.raises(ValueError):
        build_grid(m, rq(pad("S", 5, 5), pad("S", 9, 5, ref="B"), layers=("F.Cu", "F.Cu")))
    with pytest.raises(ValueError):
        build_grid(m, rq(pad("S", 5, 5), pad("S", 9, 5, ref="B"), layers=("X.Cu",)))


def test_vias_keepout_on_unrouted_layer_blocks_vias() -> None:
    from .builders import zone

    poly = ((9.0, 9.0), (11.0, 9.0), (11.0, 11.0), (9.0, 11.0))
    k = zone("K", "", ("In1.Cu",), poly, rule_area=True, keepout=frozenset({"vias"}))
    m = board(
        parts=(),
        outline=outline(0, 0, 30, 20),
        zones=(k,),
        copper_layers=("F.Cu", "In1.Cu", "B.Cu"),
    )
    g = build_grid(
        m, rq(pad("S", 5, 10), pad("S", 15, 10, ref="B"), layers=("F.Cu", "B.Cu"), allow_vias=True)
    )
    assert not g.via_ok(*g.cell(10.0, 10.0))


def test_reachable_fast_on_dense_multilayer_board() -> None:
    import time

    layers = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
    tracks = [
        track(
            f"T{k}",
            layer,
            (20.0 + 0.6 * k, 30.0),
            (20.0 + 0.6 * k, 70.0),
            0.15,
            kiid=f"t{k}{layer}",
        )
        for k in range(51)
        for layer in layers
    ]
    m = board(parts=(), outline=outline(0, 0, 100, 100), tracks=tuple(tracks), copper_layers=layers)
    from copperchat.board.router import solve

    # walled in on every layer (the way round is outside both windows): fast None
    req = rq(pad("S", 10, 50), pad("S", 60, 50, ref="B"), layers=layers, allow_vias=True)
    t = time.monotonic()
    assert solve(m, req) is None
    assert time.monotonic() - t < 20.0
    # alongside the dense field: routes
    req = rq(pad("S", 15, 27), pad("S", 55, 27, ref="B"), layers=layers, allow_vias=True)
    t = time.monotonic()
    assert solve(m, req) is not None
    assert time.monotonic() - t < 20.0


@pytest.mark.parametrize("dx", [0.02, 0.03, 0.07, 0.13, 0.31, 0.53, 0.6, 5.03, 5.01])
def test_straight_connection_stays_straight(dx: float) -> None:
    req = rq(pad("SIG", 10, 10), pad("SIG", 10 + dx, 10, ref="B"))
    path = go(req)
    if dx <= 0.25:  # the pads overlap: a direct connection or nothing
        assert path is None or path.length <= dx + 0.01
    else:
        assert path is not None
        assert path.length == pytest.approx(dx, abs=1e-6)
        assert len(path.segments) == 1


def test_deadline_stops_the_search() -> None:
    import time

    req = rq(pad("SIG", 5, 10), pad("SIG", 25, 10, ref="B"))
    m = board(
        parts=(),
        outline=outline(0, 0, 30, 20),
        tracks=(track("W", "F.Cu", (15.0, 7.0), (15.0, 13.0), 0.2),),
    )
    grid = build_grid(m, req)
    assert route(grid, req, deadline=time.monotonic() - 1.0) is None
    assert route(grid, req) is not None


@pytest.mark.parametrize(
    "change",
    [
        {"clearance": -1.0},
        {"width": float("nan")},
        {"width": 0.0},
        {"via": (0.3, 0.3)},
        {"width": float("inf")},
    ],
)
def test_request_numbers_validated(change: dict) -> None:
    from dataclasses import replace

    m = board(parts=(), outline=outline(0, 0, 30, 20))
    with pytest.raises(ValueError):
        build_grid(m, replace(rq(pad("S", 5, 5), pad("S", 9, 5, ref="B")), **change))


def test_solve_time_limit_covers_grid_building(monkeypatch: pytest.MonkeyPatch) -> None:
    from copperchat.board.router import solve

    monkeypatch.setattr(astar_mod, "SOLVE_TIME_LIMIT_S", -1.0)
    req = rq(pad("SIG", 5, 10), pad("SIG", 15, 10, ref="B"))
    m = board(parts=(), outline=outline(0, 0, 30, 20))
    assert solve(m, req) is None


def test_huge_sizes_refused() -> None:
    from dataclasses import replace

    m = board(parts=(), outline=outline(0, 0, 30, 20))
    with pytest.raises(ValueError):
        build_grid(m, replace(rq(pad("S", 5, 5), pad("S", 9, 5, ref="B")), width=1000.0))


def test_stacked_pads_on_two_layers_use_a_via() -> None:
    a = pad("SIG", 10, 10, 0.3, 0.3, layer="F.Cu")
    b = pad("SIG", 10.02, 10, 0.3, 0.3, layer="B.Cu", ref="B")
    path = go(rq(a, b, layers=("F.Cu", "B.Cu"), allow_vias=True))
    assert path is not None
    assert len(path.vias) == 1


# ---- R3 (owner rule a): T-junctions, never Y; no X; tree goals -------------------------


def test_junctions_ok_accepts_ninety_branch_off_own_copper() -> None:
    """A branch that meets a straight own track perpendicularly is a T (allowed, owner rule
    a, reverses P9a); crossing it is an X (refused, the router never makes one)."""
    from copperchat.board.router.astar import junctions_ok

    own = {"F.Cu": [((0.0, 5.0), (10.0, 5.0))]}
    t = [("F.Cu", [(5.0, 0.0), (5.0, 5.0)])]
    assert junctions_ok(t, own, lambda layer, p: False)
    cross = [("F.Cu", [(5.0, 0.0), (5.0, 10.0)])]
    assert not junctions_ok(cross, own, lambda layer, p: False)
    assert junctions_ok([("B.Cu", cross[0][1])], own, lambda layer, p: False)


def test_junctions_ok_rejects_45_branch() -> None:
    """sp4_synth's shared exit stub: a second connection that shares the stub and branches
    off it at 45° is a Y (M15) - refused; inside the pad any branch is a joint."""
    from copperchat.board.router.astar import junctions_ok

    stub_end = (136.7375, 120.325)
    own = {"F.Cu": [((137.6375, 120.325), stub_end), (stub_end, (135.8875, 119.475))]}

    def in_pad(layer: str, p: tuple[float, float]) -> bool:
        return layer == "F.Cu" and abs(p[0] - 137.6375) <= 0.7375 and abs(p[1] - 120.325) <= 0.2

    y = [("F.Cu", [(137.6375, 120.325), stub_end, (136.4375, 120.625), (136.4375, 121.425)])]
    assert not junctions_ok(y, own, in_pad)
    y2 = [("F.Cu", [(137.6375, 120.325), (136.4375, 120.325), (136.0375, 120.725)])]
    assert not junctions_ok(y2, own, in_pad)
    ending = [("F.Cu", [(3.0, 1.0), (5.0, 3.0)])]  # a 45° branch ending on a track: a Y
    assert not junctions_ok(ending, {"F.Cu": [((0.0, 3.0), (10.0, 3.0))]}, lambda la, p: False)
    pad_branch = [("F.Cu", [(137.6375, 120.325), (137.2375, 120.725)])]
    own_in_pad = {"F.Cu": [((137.6375, 120.325), (137.2375, 119.925))]}
    assert junctions_ok(pad_branch, own_in_pad, in_pad)


def test_junctions_ok_own_track_ending_on_the_path() -> None:
    """An existing own track that ends on the interior of a straight path segment at 90° is
    a T (allowed), within M4's 2 µm join tolerance; at 45° it is a Y."""
    from copperchat.board.router.astar import junctions_ok

    own = {"F.Cu": [((5.0, 3.0), (5.0, 4.9995))]}  # ends 0.5 µm above the path
    path = [("F.Cu", [(0.0, 5.0), (10.0, 5.0)])]
    assert junctions_ok(path, own, lambda layer, p: False)
    diag = {"F.Cu": [((3.0, 3.0), (5.0, 5.0))]}
    assert not junctions_ok(path, diag, lambda layer, p: False)


def _pin_rig(*, extra_pads: int = 0) -> tuple[Any, Any, Any, Any]:
    """A pin with two targets just diagonally off it (sp4_synth's shared-stub case):
    (model, pin, up, down); `extra_pads` own-net pads far away (mask cost)."""
    from tests.unit.board.builders import part
    from tests.unit.board.builders_circuit import circuit, pad

    pin = pad("U1", "1", "P", (10.0, 10.0), w=1.4, h=0.4)
    up = pad("C1", "1", "P", (7.4, 9.0), w=0.5, h=0.5)
    down = pad("C2", "1", "P", (7.4, 11.0), w=0.5, h=0.5)
    parts = [
        part("U1", 10.0, 10.0, pads=(pin,)),
        part("C1", 7.4, 9.0, pads=(up,)),
        part("C2", 7.4, 11.0, pads=(down,)),
    ]
    for k in range(extra_pads):
        x, y = 20.0 + (k % 20) * 1.0, 20.0 + (k // 20) * 1.0
        parts.append(part(f"R{k}", x, y, pads=(pad(f"R{k}", "1", "P", (x, y), w=0.5, h=0.5),)))
    return circuit(*parts), pin, up, down


def _with(m: Any, path: Any, tag: str) -> Any:
    from dataclasses import replace

    from copperchat.board.model import Track

    tracks = tuple(
        Track(f"{tag}{i}", "P", lay, a, b, w, True)
        for i, (lay, a, b, w) in enumerate(path.segments)
    )
    return replace(m, tracks=m.tracks + tracks)


def _second_route(m: Any, pin: Any, up: Any, down: Any, *, tree: bool = False) -> tuple[Any, Any]:
    """The first connection pin -> up routed; the second request: pin -> down, or with
    `tree` down -> pin (pad b = the pin, which has copper: a tree request)."""
    req = RouteRequest("P", pin, up, 0.2, 0.2, ("F.Cu",), False, (0.6, 0.3))
    first = route(build_grid(m, req), req)
    assert first is not None
    m2 = _with(m, first, "t")
    if tree:
        return m2, RouteRequest("P", down, pin, 0.2, 0.2, ("F.Cu",), False, (0.6, 0.3), tree=True)
    return m2, RouteRequest("P", pin, down, 0.2, 0.2, ("F.Cu",), False, (0.6, 0.3))


def _report(m2: Any, second: Any) -> Any:
    from copperchat.board.geom import analyse_tracks

    return analyse_tracks(_with(m2, second, "u"))


@pytest.mark.parametrize("tree", [False, True])
def test_search_branches_as_t_off_a_shared_stub(tree: bool) -> None:
    """Two connections from one pin: the second never branches off the first at 45° (a Y,
    M15) - it leaves the pad on its own or, as a tree request, lands on the first one's
    straight piece as a T; no 90° corner either."""
    m, pin, up, down = _pin_rig()
    m2, req2 = _second_route(m, pin, up, down, tree=tree)
    second = route(build_grid(m2, req2), req2)
    assert second is not None
    rep = _report(m2, second)
    assert rep.y_junctions == () and rep.ninety == () and rep.acute == (), rep


def test_search_masks_alone_avoid_the_y(monkeypatch: pytest.MonkeyPatch) -> None:
    """The move masks steer the search by themselves (junctions_ok switched off)."""
    m, pin, up, down = _pin_rig()
    m2, req2 = _second_route(m, pin, up, down)
    monkeypatch.setattr(astar_mod, "junctions_ok", lambda *a, **k: True)
    second = route(build_grid(m2, req2), req2)
    assert second is not None
    assert _report(m2, second).y_junctions == ()


def test_junction_masks_direction_convention() -> None:
    """A horizontal own track: on its nodes the vertical moves (DIRS 2 = up, 6 = down:
    leaving it sideways) and the four diagonals (a Y) are blocked; along it is free. A
    node next to it may step onto it vertically (a T arrival) but not diagonally. Its
    nodes inside an own pad of the layer are free."""
    from copperchat.board.router.astar import DIRS, junction_masks

    m, pin, up, down = _pin_rig()
    m2, req2 = _second_route(m, pin, up, down)
    grid = build_grid(m2, req2)
    masks = junction_masks(grid)
    li = grid.layers.index("F.Cu")
    blocked_on = {k for k, (di, dj) in enumerate(DIRS) if di == 0 or dj != 0}
    diagonal = {k for k, (di, dj) in enumerate(DIRS) if di and dj}
    seen = above = 0
    for t in m2.tracks:
        if abs(t.start[1] - t.end[1]) > 1e-9:
            continue  # only the horizontal stub
        lo, hi = min(t.start[0], t.end[0]) + 0.3, max(t.start[0], t.end[0]) - 0.3
        for n, bits in masks[li].items():
            x, y = grid.xy(n % grid.w, n // grid.w)
            got = {k for k in range(8) if bits >> k & 1}
            if abs(y - t.start[1]) < 1e-6 and lo < x < hi:
                assert got == blocked_on
                assert not grid.own_exempt(li, (x, y))
                seen += 1
            elif abs(y - (t.start[1] - grid.pitch)) < 1e-6 and lo < x < hi:
                down_k = next(k for k, d in enumerate(DIRS) if d == (0, 1))
                assert down_k not in got and got <= diagonal and got
                above += 1
    assert seen > 0 and above > 0
    assert junction_masks(grid) is masks  # cached on the grid


def _tree_rig(own: Any) -> tuple[Any, Any]:
    """Pad A at (10, 4) and pad B at (4, 10), net P; `own` = own tracks from B (connected
    to it). Returns (model, tree request A -> B)."""
    from dataclasses import replace

    from copperchat.board.model import Track
    from tests.unit.board.builders import part
    from tests.unit.board.builders_circuit import circuit, pad

    a = pad("U1", "1", "P", (10.0, 4.0), w=0.5, h=0.5)
    b = pad("U2", "1", "P", (4.0, 10.0), w=0.5, h=0.5)
    m = circuit(part("U1", 10.0, 4.0, pads=(a,)), part("U2", 4.0, 10.0, pads=(b,)))
    tracks = tuple(Track(f"o{k}", "P", "F.Cu", s, e, 0.2, True) for k, (s, e) in enumerate(own))
    m = replace(m, tracks=tracks)
    return m, RouteRequest("P", a, b, 0.2, 0.2, ("F.Cu",), False, (0.6, 0.3), tree=True)


def test_tree_goal_perpendicular() -> None:
    """A tree request ends on the own track connected to pad b: perpendicular, the end
    point exactly on the track, much shorter than going to pad b."""
    from copperchat.board.geom import analyse_tracks

    m, req = _tree_rig([((4.0, 10.0), (14.0, 10.0))])
    path = route(build_grid(m, req), req)
    assert path is not None
    last = path.segments[-1]
    assert abs(last[2][1] - 10.0) < 1e-6 and abs(last[1][0] - last[2][0]) < 1e-6  # vertical
    assert path.length < 6.5  # 6 mm down; pad b is ~8.5 mm away
    rep = analyse_tracks(_with(m, path, "n"))
    assert rep.y_junctions == () and rep.ninety == ()


def test_tree_goal_not_on_bend() -> None:
    """No tree goal lands within `width` of a bend vertex of the own copper (a T there
    would be a Y, M15) - nor on its free end."""
    from copperchat.board.router.astar import tree_goals

    bend = (8.0, 10.0)
    m, req = _tree_rig([((4.0, 10.0), bend), (bend, (11.0, 7.0))])
    grid = build_grid(m, req)
    goals = tree_goals(grid, req)
    lands = [land for layer in goals for _bits, land in layer.values()]
    assert lands
    assert all(math.dist(p, bend) >= req.width - 1e-9 for p in lands)
    assert all(math.dist(p, (11.0, 7.0)) >= req.width - 1e-9 for p in lands)
    path = route(grid, req)
    assert path is not None and math.dist(path.segments[-1][2], bend) >= req.width - 1e-9


def test_tree_goal_not_on_existing_junction() -> None:
    from copperchat.board.router.astar import tree_goals

    junction = (8.0, 10.0)
    m, req = _tree_rig([((4.0, 10.0), (14.0, 10.0)), (junction, (8.0, 13.0))])
    lands = [land for layer in tree_goals(build_grid(m, req), req) for _b, land in layer.values()]
    assert lands and all(math.dist(p, junction) >= req.width - 1e-9 for p in lands)


def test_tree_goal_not_on_track_end() -> None:
    from copperchat.board.router.astar import tree_goals

    m, req = _tree_rig([((4.0, 10.0), (9.0, 10.0))])
    lands = [land for layer in tree_goals(build_grid(m, req), req) for _b, land in layer.values()]
    assert lands and all(p[0] <= 9.0 - req.width + 1e-9 for p in lands)


def test_tree_goals_only_on_copper_connected_to_b() -> None:
    """Own copper that is not connected to pad b gives no goals (routing onto it would not
    connect b)."""
    from copperchat.board.router.astar import tree_goals

    m, req = _tree_rig([((5.0, 7.0), (14.0, 7.0))])  # not touching pad b
    assert all(not layer for layer in tree_goals(build_grid(m, req), req))


def test_tree_goals_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    from copperchat.board.router.astar import tree_goals

    monkeypatch.setattr(astar_mod, "TREE_GOALS_MAX", 5)
    m, req = _tree_rig([((4.0, 10.0), (14.0, 10.0))])
    got = tree_goals(build_grid(m, req), req)
    assert sum(len(layer) for layer in got) == 5


def test_own_exempt_is_per_layer() -> None:
    """An own pad on F.Cu does not exempt a B.Cu node (M4 uses the layer's pads)."""
    from dataclasses import replace

    from copperchat.board.model import Track

    m, pin, up, _down = _pin_rig()
    req = RouteRequest("P", pin, up, 0.2, 0.2, ("F.Cu", "B.Cu"), True, (0.6, 0.3))
    m2 = replace(m, tracks=(Track("b", "P", "B.Cu", (9.0, 8.0), (11.0, 8.0), 0.2, True),))
    m2 = replace(m2, copper_layers=("F.Cu", "B.Cu"))
    grid = build_grid(m2, req)
    fi, bi = grid.layers.index("F.Cu"), grid.layers.index("B.Cu")
    assert grid.own_exempt(fi, (10.0, 10.0))
    assert not grid.own_exempt(bi, (10.0, 10.0))


def test_junction_masks_cost_bounded_and_deadline() -> None:
    """Many own segments and pads: the masks stay cheap (each segment only checks the
    spots near it), and a passed deadline stops them."""
    import time as _time
    from dataclasses import replace

    from copperchat.board.model import Track
    from copperchat.board.router.astar import junction_masks
    from copperchat.board.router.grid import DeadlineExpired

    m, pin, up, _down = _pin_rig(extra_pads=200)
    tracks = tuple(
        Track(f"s{k}", "P", "F.Cu", (20.0, 20.0 + k), (40.0, 20.0 + k), 0.2, True)
        for k in range(40)
    )
    m2 = replace(m, tracks=tracks)
    req = RouteRequest("P", pin, up, 0.2, 0.2, ("F.Cu",), False, (0.6, 0.3))
    grid = build_grid(m2, req, window_mm=40.0)
    t0 = _time.perf_counter()
    junction_masks(grid)
    assert _time.perf_counter() - t0 < 1.0
    fresh = build_grid(m2, req, window_mm=40.0)
    with pytest.raises(DeadlineExpired):
        junction_masks(fresh, deadline=_time.monotonic() - 1.0)


@pytest.mark.parametrize("offset", [0.0, 0.025, 0.0125])
def test_search_never_crosses_own_copper(offset: float) -> None:
    """Own copper not connected to pad b - on the request's lattice or not - is never
    crossed (an X, or at 45° a Y): the search goes around it; as a tree request (the track
    connected to b) it lands on it as a T."""
    import time as _time
    from dataclasses import replace

    from copperchat.board.geom import analyse_tracks
    from copperchat.board.model import Track
    from copperchat.board.router.astar import junctions_ok
    from tests.unit.board.builders import part
    from tests.unit.board.builders_circuit import circuit, pad

    a = pad("U1", "1", "P", (10.0, 10.0), w=0.5, h=0.5)
    b = pad("U2", "1", "P", (10.0, 14.0), w=0.5, h=0.5)
    m = circuit(part("U1", 10.0, 10.0, pads=(a,)), part("U2", 10.0, 14.0, pads=(b,)))
    y = 12.0 + offset
    m = replace(m, tracks=(Track("t", "P", "F.Cu", (7.0, y), (13.0, y), 0.2, True),))
    req = RouteRequest("P", a, b, 0.2, 0.2, ("F.Cu",), False, (0.6, 0.3))
    t0 = _time.perf_counter()
    path = route(build_grid(m, req), req)
    assert _time.perf_counter() - t0 < 2.0
    assert path is not None
    own = {"F.Cu": [((7.0, y), (13.0, y))]}
    pts = [path.segments[0][1], *(q for _, _, q, _w in path.segments)]
    assert junctions_ok([("F.Cu", pts)], own, lambda layer, p: False)
    assert analyse_tracks(_with(m, path, "n")).y_junctions == ()
    # connected to b: a T onto it
    m_t = replace(
        m, tracks=(*m.tracks, Track("s", "P", "F.Cu", (10.0, 14.0), (10.0, 12.5), 0.2, True))
    )
    m_t = replace(
        m_t, tracks=(Track("t", "P", "F.Cu", (7.0, 12.5), (13.0, 12.5), 0.2, True), m_t.tracks[1])
    )
    req_t = replace(req, tree=True)
    path_t = route(build_grid(m_t, req_t), req_t)
    assert path_t is not None and abs(path_t.segments[-1][2][1] - 12.5) < 1e-6
    rep = analyse_tracks(_with(m_t, path_t, "n"))
    assert rep.y_junctions == () and rep.ninety == ()


def test_junction_masks_fast_with_long_diagonal_own_tracks() -> None:
    """Long diagonal own tracks over many own pads: the exemption lookup is bucketed, so
    the masks stay well below the router's time limit."""
    import time as _time
    from dataclasses import replace

    from copperchat.board.model import Track
    from copperchat.board.router.astar import junction_masks

    m, pin, up, _down = _pin_rig(extra_pads=300)
    tracks = tuple(
        Track(f"d{k}", "P", "F.Cu", (5.0 + k * 0.5, 5.0), (45.0 + k * 0.5, 45.0), 0.2, True)
        for k in range(60)
    )
    m2 = replace(m, tracks=tracks)
    req = RouteRequest("P", pin, up, 0.2, 0.2, ("F.Cu",), False, (0.6, 0.3))
    grid = build_grid(m2, req, window_mm=40.0)
    t0 = _time.perf_counter()
    junction_masks(grid)
    # a coarse ceiling (≈ 1 s on a dev box; the unbucketed lookup took 23.6 s)
    assert _time.perf_counter() - t0 < 3.0
