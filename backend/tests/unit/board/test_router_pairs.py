"""Coupled pairs and the worker entry points (P5a Task 4)."""

from __future__ import annotations

import math
import pickle
import random
from dataclasses import replace
from typing import Any

import pytest

from copperchat.board.geom import analyse_bends
from copperchat.board.model import BoardModel, Pad
from copperchat.board.router import (
    RoutePath,
    RouteRequest,
    build_grid,
    pair_coupling,
    prune_model,
    route,
    route_pair,
    solve,
    solve_pair,
)
from copperchat.board.router import astar as astar_mod
from copperchat.board.router import pairs as pairs_mod
from copperchat.board.router.simplify import layer_runs

from .builders import board, outline, part, track
from .router_maps import exact_clearance_ok, perf_board, random_map, seg_dist


def pad(net: str, x: float, y: float, size: float = 0.2, ref: str = "J1", number: str = "1") -> Pad:
    return Pad(
        ref, number, net, x, y, size, size, 0.0, "rect", frozenset({"F.Cu", "F.Paste"}), True, None
    )


def req(
    net: str, a: Pad, b: Pad, *, width: float = 0.2, clearance: float = 0.2, layers=("F.Cu",)
) -> RouteRequest:
    return RouteRequest(net, a, b, width, clearance, layers, len(layers) > 1, (0.6, 0.3))


def pair_board(pads: list[Pad], tracks=(), w: float = 30, h: float = 20) -> BoardModel:
    parts = tuple(part(f"X{k}", p.x, p.y, pads=(p,)) for k, p in enumerate(pads))
    return board(parts=parts, tracks=tuple(tracks), outline=outline(0, 0, w, h))


def clean(path: RoutePath, width: float) -> None:
    runs = layer_runs(path)
    for k, (_l, pts) in enumerate(runs):
        r = analyse_bends(pts, width, pad_ends=(k == 0, k == len(runs) - 1))
        assert not (
            r.non_octilinear
            or r.ninety
            or r.acute
            or r.off_angle
            or r.micro_jogs
            or r.short_chamfers
        ), r


def min_dist(a: RoutePath, b: RoutePath) -> float:
    return min(
        seg_dist(p, q, s, e) for la, p, q in a.segments for lb, s, e in b.segments if la == lb
    )


def straight_pair(**kw: Any):
    pa, pb = pad("DP", 5, 10, ref="J1", number="1"), pad("DP", 15, 10, ref="U1", number="1")
    na, nb = pad("DN", 5, 10.4, ref="J1", number="2"), pad("DN", 15, 10.4, ref="U1", number="2")
    m = pair_board([pa, pb, na, nb], **kw)
    return m, req("DP", pa, pb), req("DN", na, nb)


def test_coupled_straight() -> None:
    m, p, n = straight_pair()
    got = route_pair(m, p, n, 0.2)
    assert got is not None
    pp, pn = got
    c = pair_coupling(pp, pn)
    assert c["gap_max"] <= 0.41
    for path, r in ((pp, p), (pn, n)):
        clean(path, 0.2)
        assert path.segments[0][1] == (r.a.x, r.a.y) and path.segments[-1][2] == (r.b.x, r.b.y)


def test_coupled_one_bend() -> None:
    pa, na = pad("DP", 5, 10, number="1"), pad("DN", 5, 10.4, number="2")
    pb, nb = pad("DP", 15, 6.0, ref="U1", number="1"), pad("DN", 15, 6.4, ref="U1", number="2")
    m = pair_board([pa, pb, na, nb])
    p, n = req("DP", pa, pb), req("DN", na, nb)
    got = route_pair(m, p, n, 0.2)
    assert got is not None
    pp, pn = got
    clean(pp, 0.2)
    clean(pn, 0.2)
    bent = [pts for _l, pts in layer_runs(pp) if len(pts) >= 3]
    assert bent, pp  # the pair bends
    c = pair_coupling(pp, pn)
    assert c["gap_min"] >= 0.4 - 1e-6 and c["gap_max"] <= 0.41
    assert min_dist(pp, pn) >= 0.4 - 1e-6  # clearance + w between the centre lines
    assert exact_clearance_ok(m, pp, p) and exact_clearance_ok(m, pn, n)


def test_fallback_when_far_apart() -> None:
    pa, na = pad("DP", 5, 5, number="1"), pad("DN", 5, 11, number="2")
    pb, nb = pad("DP", 20, 5, ref="U1", number="1"), pad("DN", 20, 11, ref="U1", number="2")
    m = pair_board([pa, pb, na, nb])
    p, n = req("DP", pa, pb), req("DN", na, nb)
    assert not pairs_mod.coupled_eligible(p, n, 0.2)
    got = route_pair(m, p, n, 0.2)
    assert got is not None
    pp, pn = got
    clean(pp, 0.2)
    clean(pn, 0.2)
    assert min_dist(pp, pn) >= 0.2 + 0.2 - 1e-6  # clearance + w


def test_fallback_band_marks_coupling_distance() -> None:
    from copperchat.board.router.grid import band_cells

    m, p, _n = straight_pair()
    grid = build_grid(m, p)
    seg = (("F.Cu", (5.0, 10.0), (15.0, 10.0)),)
    band = band_cells(grid, seg, 0.4 - grid.pitch, 0.4 + grid.pitch)[0]

    def at(x: float, y: float) -> int:
        i, j = grid.cell(x, y)
        return band[j * grid.w + i]

    assert at(10.0, 10.4) == 1 and at(10.0, 10.35) == 1 and at(10.0, 10.45) == 1
    assert at(10.0, 9.6) == 1
    assert at(10.0, 10.3) == 0 and at(10.0, 10.5) == 0 and at(10.0, 10.0) == 0


def test_pair_none_when_blocked() -> None:
    wall = track("W", "F.Cu", (10.0, -1.0), (10.0, 21.0), 0.2)
    m, p, n = straight_pair(tracks=(wall,))
    assert route_pair(m, p, n, 0.2) is None
    assert solve_pair(m, p, n, 0.2) is None


def test_solve_picklable() -> None:
    m, p, _n = straight_pair(tracks=(track("W", "F.Cu", (10.0, 8.0), (10.0, 12.0), 0.2),))
    m2, p2 = pickle.loads(pickle.dumps((m, p)))
    assert (m2, p2) == (m, p)
    got = solve(m2, p2)
    assert got is not None
    assert got == route(build_grid(m, p), p)
    assert pickle.loads(pickle.dumps(got)) == got


def test_pair_coupling_keys() -> None:
    m, p, n = straight_pair()
    pp, pn = route_pair(m, p, n, 0.2)
    c = pair_coupling(pp, pn)
    assert set(c) == {"length_mismatch", "gap_min", "gap_max"}
    assert c["gap_max"] - c["gap_min"] <= 0.02
    assert abs(c["gap_min"] - (0.2 + 0.2)) <= 0.01
    assert c["length_mismatch"] < 0.1
    lone = RoutePath((("B.Cu", (0.0, 0.0), (1.0, 0.0)),), (), 1.0)
    far = pair_coupling(pp, lone)
    assert far["gap_min"] == math.inf and far["gap_max"] == math.inf


def _spy(monkeypatch: pytest.MonkeyPatch) -> list[float | None]:
    calls: list[float | None] = []
    real = astar_mod.build_grid

    def spy(model, r, **kw):  # type: ignore[no-untyped-def]
        calls.append(kw.get("window_mm"))
        return real(model, r, **kw)

    monkeypatch.setattr(astar_mod, "build_grid", spy)
    monkeypatch.setattr(pairs_mod, "build_grid", spy)
    return calls


def _detour_board(full: bool) -> tuple[BoardModel, RouteRequest]:
    # a wall at x = 12 from the top edge down to y = 17 (or the whole height): the way
    # round lies outside the 6 mm window (y 4..16) but inside the 12 mm one (y 0..22)
    a, b = pad("S", 10, 10, 0.5), pad("S", 14, 10, 0.5, ref="U1")
    wall = track("W", "F.Cu", (12.0, -1.0), (12.0, 31.0 if full else 17.0), 0.2)
    return pair_board([a, b], tracks=(wall,), w=30, h=30), req("S", a, b)


def test_window_doubles_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch)
    m, r = _detour_board(full=False)
    assert solve(m, r) is not None
    assert calls == [None, 12.0]
    calls.clear()
    m, r = _detour_board(full=True)
    assert solve(m, r) is None
    assert calls == [None, 12.0]


def test_solve_pair_fallback_window_doubles_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch)
    pa, na = pad("DP", 10, 10, 0.5, number="1"), pad("DN", 10, 20, 0.5, number="2")
    pb, nb = (
        pad("DP", 14, 10, 0.5, ref="U1", number="1"),
        pad("DN", 14, 20, 0.5, ref="U1", number="2"),
    )
    wall = track("W", "F.Cu", (12.0, -1.0), (12.0, 31.0), 0.2)
    m = pair_board([pa, pb, na, nb], tracks=(wall,), w=30, h=30)
    p, n = req("DP", pa, pb), req("DN", na, nb)
    assert solve_pair(m, p, n, 0.2) is None
    assert calls == [None, 12.0]  # p fails twice; n is never tried


def test_solve_pair_margin() -> None:
    obstacle = track("W", "F.Cu", (6.0, 11.2), (14.0, 11.2), 0.2)
    m, p, n = straight_pair(tracks=(obstacle,))
    base = solve_pair(m, p, n, 0.2)
    got = solve_pair(m, p, n, 0.2, margin=0.1)
    assert base is not None and got is not None
    assert pair_coupling(*got)["gap_min"] == pytest.approx(
        pair_coupling(*base)["gap_min"], abs=1e-6
    )
    # every segment keeps clearance + 0.1 from the other copper (the partner keeps the gap)
    only_obstacle = replace(m, parts=())
    for path, r in zip(got, (p, n), strict=True):
        assert exact_clearance_ok(only_obstacle, path, r, margin=0.1)


@pytest.mark.parametrize("seed", range(12))
def test_prune_model_same_result(seed: int) -> None:
    m, reqs = random_map(random.Random(seed))
    for r in reqs:
        pruned = prune_model(m, r)
        assert pruned.outline == m.outline
        assert len(pruned.parts) <= len(m.parts) and len(pruned.tracks) <= len(m.tracks)
        assert solve(pruned, r) == solve(m, r)


def test_prune_model_drops_far_items() -> None:
    m, reqs = perf_board(
        random.Random(777), size=100, n_parts=80, n_nets=5, layers=("F.Cu", "B.Cu"), net_len=(5, 10)
    )
    r = reqs[0]
    pruned = prune_model(m, r)
    n_pads = sum(len(p.pads) for p in m.parts)
    assert sum(len(p.pads) for p in pruned.parts) < n_pads / 2
    assert solve(pruned, r) == solve(m, r)
    pair = prune_model(m, (reqs[0], reqs[1]))
    assert sum(len(p.pads) for p in pair.parts) >= sum(len(p.pads) for p in pruned.parts)


def _pad_on(net: str, x: float, y: float, layer: str, ref: str = "J1", number: str = "1") -> Pad:
    return Pad(ref, number, net, x, y, 0.2, 0.2, 0.0, "rect", frozenset({layer}), True, None)


def test_coupled_on_the_pads_layer() -> None:
    pads = [
        _pad_on("DP", 5, 10, "B.Cu", number="1"),
        _pad_on("DP", 15, 10, "B.Cu", "U1", "1"),
        _pad_on("DN", 5, 10.4, "B.Cu", number="2"),
        _pad_on("DN", 15, 10.4, "B.Cu", "U1", "2"),
    ]
    m = pair_board(pads)
    p = req("DP", pads[0], pads[1], layers=("F.Cu", "B.Cu"))
    n = req("DN", pads[2], pads[3], layers=("F.Cu", "B.Cu"))
    got = route_pair(m, p, n, 0.2)
    assert got is not None
    for path in got:
        assert path.segments[0][0] == "B.Cu" and path.segments[-1][0] == "B.Cu"


def test_fallback_partner_vias_keep_clear() -> None:
    # a full-height F.Cu wall: both nets need vias; their vias must not touch
    pa, na = pad("DP", 5, 5.0, 0.3, number="1"), pad("DN", 5, 5.6, 0.3, number="2")
    pb, nb = (
        pad("DP", 17, 5.0, 0.3, ref="U1", number="1"),
        pad("DN", 17, 5.6, 0.3, ref="U1", number="2"),
    )
    wall = track("W", "F.Cu", (11.0, -1.0), (11.0, 21.0), 0.2)
    m = pair_board([pa, pb, na, nb], tracks=(wall,))
    p = req("DP", pa, pb, layers=("F.Cu", "B.Cu"))
    n = req("DN", na, nb, layers=("F.Cu", "B.Cu"))
    got = solve_pair(m, p, n, 0.2)
    assert got is not None
    pp, pn = got
    assert pp.vias and pn.vias
    for v in pp.vias:
        for u in pn.vias:
            assert math.dist(v, u) >= 0.6 + 0.2 - 1e-6  # two via radii + pair clearance
        for layer, s, e in pn.segments:
            assert seg_dist(v, v, s, e) >= 0.3 + 0.2 + 0.1 - 1e-6, layer
    assert min_dist(pp, pn) >= 0.4 - 1e-6


@pytest.mark.parametrize("xb", [10.5, 11.0, 12.0])
def test_coupled_reversal_does_not_crash(xb: float) -> None:
    pa, na = pad("DP", 10, 10, number="1"), pad("DN", 10, 10.4, number="2")
    pb, nb = pad("DP", xb, 10, ref="U1", number="1"), pad("DN", xb, 10.4, ref="U1", number="2")
    m = pair_board([pa, pb, na, nb])
    got = route_pair(m, req("DP", pa, pb), req("DN", na, nb), 0.2)
    if got is not None:
        clean(got[0], 0.2)
        clean(got[1], 0.2)


@pytest.mark.parametrize("pitch", [0.5, 0.65])
def test_coupled_with_pad_pitch_wider_than_pair(pitch: float) -> None:
    pa, na = pad("DP", 5, 10, number="1"), pad("DN", 5, 10 + pitch, number="2")
    pb, nb = (
        pad("DP", 15, 10, ref="U1", number="1"),
        pad("DN", 15, 10 + pitch, ref="U1", number="2"),
    )
    m = pair_board([pa, pb, na, nb])
    p, n = req("DP", pa, pb), req("DN", na, nb)
    got = route_pair(m, p, n, 0.2)
    assert got is not None
    pp, pn = got
    clean(pp, 0.2)
    clean(pn, 0.2)
    c = pair_coupling(pp, pn)
    assert c["gap_min"] == pytest.approx(0.4, abs=1e-6)  # coupled at gap + w in the middle
    assert exact_clearance_ok(m, pp, p) and exact_clearance_ok(m, pn, n)
    assert min_dist(pp, pn) >= 0.4 - 1e-6


def _strict(path: RoutePath, r: RouteRequest) -> None:
    from .router_maps import strict_bends_ok

    runs = layer_runs(path)
    for k, (_l, pts) in enumerate(runs):
        pads = (r.a if k == 0 else None, r.b if k == len(runs) - 1 else None)
        assert strict_bends_ok(pts, r.width, pads), pts


def test_coupled_no_micro_jog_outside_the_pads() -> None:
    # b pads 0.1 mm off the a pads' line: no 45-degree jog shorter than JOG_MIN outside a pad
    pa, na = pad("DP", 5, 10, number="1"), pad("DN", 5, 10.4, number="2")
    pb, nb = pad("DP", 15, 9.9, ref="U1", number="1"), pad("DN", 15, 10.3, ref="U1", number="2")
    m = pair_board([pa, pb, na, nb])
    p, n = req("DP", pa, pb), req("DN", na, nb)
    got = route_pair(m, p, n, 0.2)
    assert got is not None
    _strict(got[0], p)
    _strict(got[1], n)


def test_fan_in_never_leaves_a_short_chamfer() -> None:
    pa = pad("DP", 5, 10, 1.905, number="1")
    na = pad("DN", 5, 12.54, 1.905, number="2")
    pb = pad("DP", 8, 10.27, 1.905, ref="U1", number="1")
    nb = pad("DN", 8, 12.81, 1.905, ref="U1", number="2")
    m = pair_board([pa, pb, na, nb])
    p, n = req("DP", pa, pb), req("DN", na, nb)
    got = route_pair(m, p, n, 0.2)
    if got is not None:
        _strict(got[0], p)
        _strict(got[1], n)


def test_coupled_avoids_partner_net_copper() -> None:
    # existing DN copper across DP's straight line: no short with it
    dn_stub = track("DN", "F.Cu", (10.0, 7.0), (10.0, 10.05), 0.2, kiid="dn-stub")
    m, p, n = straight_pair(tracks=(dn_stub,))
    # the merged-net centreline runs straight through the stub; the check must reject it
    assert pairs_mod._coupled(m, p, n, 0.2, 0.0) is None
    for got in (route_pair(m, p, n, 0.2), solve_pair(m, p, n, 0.2)):
        assert got is not None
        pp, _pn = got
        for layer, s, e in pp.segments:
            if layer == "F.Cu":
                assert seg_dist(s, e, dn_stub.start, dn_stub.end) >= 0.2 + 0.2 - 1e-6


def test_coupled_rejected_when_pad_exits_fail() -> None:
    # tiny 0.18 x 0.1 pads whose long axis runs along the pair: the leads would leave
    # through the long side, so the coupled attempt must give up
    def tiny(net: str, x: float, y: float, ref: str, number: str) -> Pad:
        return Pad(ref, number, net, x, y, 0.18, 0.1, 0.0, "rect", frozenset({"F.Cu"}), True, None)

    pa, na = tiny("DP", 5, 10, "J1", "1"), tiny("DN", 5.4, 10, "J1", "2")
    pb, nb = tiny("DP", 5, 16, "U1", "1"), tiny("DN", 5.4, 16, "U1", "2")
    m = pair_board([pa, pb, na, nb])
    p = req("DP", pa, pb, clearance=0.1)
    n = req("DN", na, nb, clearance=0.1)
    assert pairs_mod._coupled(m, p, n, 0.2, 0.0) is None


def test_solve_pair_time_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pairs_mod, "SOLVE_TIME_LIMIT_S", -1.0)
    m, p, n = straight_pair()
    assert solve_pair(m, p, n, 0.2) is None
    assert route_pair(m, p, n, 0.2) is None


def test_fallback_grid_builds_honour_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from copperchat.board.router.grid import DeadlineExpired

    # skip the pre-build check: the build itself must stop at the passed deadline
    monkeypatch.setattr(pairs_mod, "check_deadline", lambda _d: None)
    m, p, _n = straight_pair()
    with pytest.raises(DeadlineExpired):
        pairs_mod._solo(m, p, 0.0, True, deadline=time.monotonic() - 1.0)
