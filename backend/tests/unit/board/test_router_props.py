"""Property and determinism tests of the router on 250 seeded random maps (P5a Task 5).

Every map is routed once (module fixture, single search per request on the default
window) and the properties are checked on the cached results; the determinism test routes
every tenth map again."""

from __future__ import annotations

import random

import pytest

from copperchat.board.geom import analyse_bends
from copperchat.board.model import BoardModel
from copperchat.board.router import RoutePath, RouteRequest, build_grid, route
from copperchat.board.router.simplify import layer_runs

from .router_maps import exact_clearance_ok, open_map, random_map

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
