"""Property and determinism tests of the router on 250 seeded random maps (P5a Task 5).

Every map is routed once (module fixture, single search per request on the default
window) and the properties are checked on the cached results; the determinism test routes
every tenth map again."""

from __future__ import annotations

import math
import random
from dataclasses import replace

import pytest

from copperchat.board.geom import analyse_bends
from copperchat.board.model import BoardModel
from copperchat.board.router import RoutePath, RouteRequest, build_grid, route, route_pair
from copperchat.board.router.simplify import layer_runs

from .router_maps import (
    exact_clearance_ok,
    open_map,
    pair_map,
    random_map,
    seg_dist,
    strict_bends_ok,
)

SEEDS = range(250)
BUDGET = 100_000  # expansions per request: the properties concern returned paths

Result = tuple[int, BoardModel, RouteRequest, RoutePath | None]


def _route(m: BoardModel, r: RouteRequest) -> RoutePath | None:
    return route(build_grid(m, r), r, max_expansions=BUDGET)


@pytest.fixture(scope="module")
def routed() -> list[Result]:
    out: list[Result] = []
    for seed in SEEDS:
        m, reqs = random_map(random.Random(seed))
        out.extend((seed, m, r, _route(m, r)) for r in reqs)
    return out


def test_maps_mostly_route(routed: list[Result]) -> None:
    found = sum(p is not None for *_rest, p in routed)
    assert len(routed) >= 400
    assert found >= 0.9 * len(routed), f"{found}/{len(routed)} routed"


def test_output_m4_m7_zero(routed: list[Result]) -> None:
    for seed, _m, r, path in routed:
        if path is None:
            continue
        runs = layer_runs(path)
        for k, (_layer, pts) in enumerate(runs):
            b = analyse_bends(pts, r.width, pad_ends=(k == 0, k == len(runs) - 1))
            bad = (b.ninety, b.acute, b.off_angle, b.micro_jogs, b.short_chamfers, b.non_octilinear)
            assert all(len(x) == 0 for x in bad), f"seed {seed} {r.net}: {b}"


def test_no_obstacle_hit(routed: list[Result]) -> None:
    for seed, m, r, path in routed:
        if path is not None:
            assert exact_clearance_ok(m, path, r), f"seed {seed} {r.net}"


def test_exact_pad_centres(routed: list[Result]) -> None:
    for seed, _m, r, path in routed:
        if path is None:
            continue
        assert path.segments[0][1] == (round(r.a.x, 6), round(r.a.y, 6)), f"seed {seed}"
        assert path.segments[-1][2] == (round(r.b.x, 6), round(r.b.y, 6)), f"seed {seed}"
        for (_l1, _p, q), (_l2, p2, _q) in zip(path.segments, path.segments[1:], strict=False):
            assert q == p2, f"seed {seed}: path not continuous"


def test_open_maps_always_route() -> None:
    for seed in range(0, 250, 5):
        m, reqs = open_map(random.Random(seed))
        for r in reqs:
            assert _route(m, r) is not None, f"seed {seed} {r.net}"


def test_deterministic(routed: list[Result]) -> None:
    for seed, _m, r, path in routed:
        if seed % 10:
            continue
        m, reqs = random_map(random.Random(seed))
        again = {q.net: _route(m, q) for q in reqs}
        assert again[r.net] == path, f"seed {seed} {r.net}"


def test_output_strict_independent(routed: list[Result]) -> None:
    """An independent checker (no geom.analyse_bends) agrees on every layer run."""
    for seed, _m, r, path in routed:
        if path is None:
            continue
        runs = layer_runs(path)
        for k, (_layer, pts) in enumerate(runs):
            pads = (r.a if k == 0 else None, r.b if k == len(runs) - 1 else None)
            assert strict_bends_ok(pts, r.width, pads), f"seed {seed} {r.net}: {pts}"


@pytest.mark.parametrize("seed", range(0, 60, 2))
def test_pairs_property(seed: int) -> None:
    try:
        m, p, n, gap = pair_map(random.Random(1000 + seed))
    except RuntimeError:  # no free spot for the pair on this map
        return
    got = route_pair(m, p, n, gap)
    if got is None:
        return
    pp, pn = got
    pc = max(gap, p.clearance, n.clearance)
    for path, r in ((pp, p), (pn, n)):
        assert exact_clearance_ok(replace(m, parts=_without(m, (p.net, n.net))), path, r), seed
        runs = layer_runs(path)
        for k, (_layer, pts) in enumerate(runs):
            pads = (r.a if k == 0 else None, r.b if k == len(runs) - 1 else None)
            assert strict_bends_ok(pts, r.width, pads), (seed, pts)
        assert path.segments[0][1] == (round(r.a.x, 6), round(r.a.y, 6))
        assert path.segments[-1][2] == (round(r.b.x, 6), round(r.b.y, 6))
    from .router_maps import pad_corners, seg_poly_dist

    for path, r, other in ((pp, p, n), (pn, n, p)):  # each path vs the partner's pads
        for layer, s0, s1 in path.segments:
            for q in (other.a, other.b):
                if layer in q.layers:
                    assert seg_poly_dist(s0, s1, pad_corners(q)) >= r.clearance + r.width / 2 - 1e-6, seed
    for la, a0, a1 in pp.segments:
        for lb, b0, b1 in pn.segments:
            if la == lb:
                assert seg_dist(a0, a1, b0, b1) >= pc + (p.width + n.width) / 2 - 1e-6, seed
    for v in pp.vias:
        assert all(
            seg_dist(v, v, s, e) >= pc + 0.3 + n.width / 2 - 1e-6 for _l, s, e in pn.segments
        )
        assert all(math.dist(v, u) >= pc + 0.6 - 1e-6 for u in pn.vias)


def _without(m: BoardModel, nets: tuple[str, str]) -> tuple:
    """The parts with the pair's own pads removed (the partner keeps the pair clearance,
    checked separately)."""
    return tuple(replace(pt, pads=tuple(q for q in pt.pads if q.net not in nets)) for pt in m.parts)
