"""Simplify and exact pad approach (P5a Task 3)."""

from __future__ import annotations

import itertools
import math

import pytest

from copperchat.board.geom import analyse_bends
from copperchat.board.model import Pad
from copperchat.board.router import RouteRequest, build_grid
from copperchat.board.router.simplify import (
    SnapError,
    layer_runs,
    merge_collinear,
    snap_ends,
    to_path,
)

from .builders import board, outline


def _clean(pts, width: float = 0.2) -> None:
    r = analyse_bends(pts, width, pad_ends=(True, True))
    assert not (r.non_octilinear or r.ninety or r.acute or r.off_angle), r
    assert not (r.micro_jogs or r.short_chamfers), r


def _octilinear(p, q) -> bool:
    ang = math.degrees(math.atan2(q[1] - p[1], q[0] - p[0])) % 45.0
    return min(ang, 45.0 - ang) < 1e-4


def test_merge_collinear() -> None:
    pts = [(0, 0), (1, 0), (1, 0), (2, 0), (3, -1), (4, -2), (4, -3)]
    assert merge_collinear(pts) == ((0, 0), (2, 0), (4, -2), (4, -3))
    assert merge_collinear([(0, 0)]) == ((0, 0),)


def test_snap_moves_last_bend() -> None:
    pts = [(0.0, 0.0), (5.0, 0.0), (6.0, -1.0)]
    out = snap_ends(pts, (0.0, 0.0), (6.013, -1.0), 0.2)
    assert out == ((0.0, 0.0), (5.013, 0.0), (6.013, -1.0))
    assert all(_octilinear(p, q) for p, q in itertools.pairwise(out))
    _clean(out)


def test_snap_head_and_tail() -> None:
    pts = [(0.0, 0.0), (3.0, 0.0), (4.0, 1.0), (8.0, 1.0)]
    out = snap_ends(pts, (0.0, 0.021), (8.0, 0.987), 0.2)
    assert out[0] == (0.0, 0.021) and out[-1] == (8.0, 0.987)
    assert all(_octilinear(p, q) for p, q in itertools.pairwise(out))
    _clean(out)


def test_snap_stub_when_parallel() -> None:
    # a single straight segment: no bend to move -> stub from the grid end to the centre
    target = (5.02, 0.013)
    out = snap_ends([(0.0, 0.0), (5.0, 0.0)], None, target, 0.2)
    assert out[-1] == target
    assert len(out) >= 3
    assert all(_octilinear(p, q) for p, q in itertools.pairwise(out))
    _clean(out)
    # the stub stays inside a 0.5 mm pad around the centre
    for p in out[out.index((5.0, 0.0)) :]:
        assert abs(p[0] - target[0]) <= 0.25 and abs(p[1] - target[1]) <= 0.25


def test_snap_never_creates_short_chamfer() -> None:
    # 0 -> 45 -> 90 degrees with a 0.283 mm chamfer piece; moving the last bend left by
    # 0.03 mm would shorten it to 0.24 < CHAMFER_MIN(0.2) = 0.25 -> a stub instead
    pts = [(0.0, 0.0), (2.0, 0.0), (2.2, -0.2), (2.2, -3.0)]
    out = snap_ends(pts, None, (2.17, -3.01), 0.2)
    assert out[:3] == ((0.0, 0.0), (2.0, 0.0), (2.2, -0.2))
    assert out[-1] == (2.17, -3.01)
    assert all(_octilinear(p, q) for p, q in itertools.pairwise(out))
    _clean(out)


def test_snap_impossible_raises() -> None:
    # the centre lies behind the segment's start: any approach turns by 180 degrees
    with pytest.raises(SnapError):
        snap_ends([(0.0, 0.0), (5.0, 0.0)], None, (-0.02, 0.0), 0.2)


def test_snap_collinear_target_moves_end_point() -> None:
    # the goal node lies past the centre on the same line: shorten, no bump
    assert snap_ends([(0.0, 0.0), (5.05, 0.0)], None, (5.03, 0.0), 0.2) == ((0.0, 0.0), (5.03, 0.0))
    out = snap_ends([(0.0, 0.0), (3.0, 0.0), (4.0, -1.0)], None, (3.98, -0.98), 0.2)
    assert out == ((0.0, 0.0), (3.0, 0.0), (3.98, -0.98))


def _req(layers=("F.Cu", "B.Cu")) -> RouteRequest:
    def pad(x: float, y: float) -> Pad:
        return Pad("A", "1", "SIG", x, y, 0.5, 0.5, 0.0, "rect", frozenset({"*.Cu"}), False, 0.3)

    return RouteRequest("SIG", pad(5.0, 10.0), pad(9.0, 10.0), 0.2, 0.2, layers, True, (0.6, 0.3))


def test_vias_at_layer_changes() -> None:
    req = _req()
    grid = build_grid(board(parts=(), outline=outline(0, 0, 20, 20)), req)
    i0, j0 = grid.cell(5.0, 10.0)
    states = [(0, i0 + k, j0) for k in range(0, 41)]  # 5.0 .. 7.0 on F.Cu
    states += [(1, i0 + 40, j0)]  # via at 7.0
    states += [(1, i0 + k, j0) for k in range(41, 81)]  # 7.05 .. 9.0 on B.Cu
    path = to_path(states, grid, req)
    assert path.vias == ((7.0, 10.0),)
    assert path.segments == (("F.Cu", (5.0, 10.0), (7.0, 10.0)), ("B.Cu", (7.0, 10.0), (9.0, 10.0)))
    assert [layer for layer, _pts in layer_runs(path)] == ["F.Cu", "B.Cu"]


def test_length_sum() -> None:
    req = _req(("F.Cu",))
    grid = build_grid(board(parts=(), outline=outline(0, 0, 20, 20)), req)
    i0, j0 = grid.cell(5.0, 10.0)
    states = [(0, i0 + k, j0) for k in range(0, 21)]  # to 6.0
    states += [(0, i0 + 20 + k, j0 - k) for k in range(1, 11)]  # diagonal 0.5 mm in x
    states += [(0, i0 + 30 + k, j0 - 10) for k in range(1, 31)]  # to 8.0, y = 9.5
    states += [(0, i0 + 60 + k, j0 - 10 + k) for k in range(1, 11)]  # back down to 10.0
    states += [(0, i0 + 70 + k, j0) for k in range(1, 11)]  # to 9.0
    path = to_path(states, grid, req)
    assert path.length == pytest.approx(sum(math.dist(p, q) for _l, p, q in path.segments))
    assert path.length == pytest.approx(3.0 + 2 * 0.5 * math.sqrt(2))
    assert path.segments[0][1] == (5.0, 10.0) and path.segments[-1][2] == (9.0, 10.0)


def test_stub_never_makes_a_tiny_chamfer() -> None:
    # review repro: a stub would put a 2 um axis piece between two same-sense 45° pieces
    pts = [(3.375284, 16.068592), (7.075284, 19.768592), (7.625284, 20.318592)]
    out = snap_ends(pts, None, (7.606301, 20.339722), 0.25)
    assert out[-1] == (7.606301, 20.339722)
    r = analyse_bends(out, 0.25, pad_ends=(False, False))
    assert not (r.short_chamfers or r.ninety or r.acute or r.off_angle), (out, r)
