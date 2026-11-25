"""Path simplification and the exact pad approach (B-C12, P5a Task 3).

The search runs on lattice nodes; the pads are not. `snap_ends` replaces a grid end node by
the real pad centre while keeping every segment octilinear: the last bend moves along the
previous segment's line (the end segment keeps its direction), or — when that is impossible
or would shorten a piece below `CHAMFER_MIN` / `JOG_MIN` — a short stub (one 45° piece plus
one axis-parallel piece, |stub| ≤ the node's distance to the centre) is added.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence

from copperchat.board.geom import CHAMFER_MIN, JOG_MIN
from copperchat.board.model import Point
from copperchat.board.router.grid import ObstacleGrid, RoutePath, RouteRequest

_TINY = 1e-9
_ROUND = 6  # output rounded to 1 nm


class SnapError(ValueError):
    """No octilinear approach to the pad centre from this grid end."""


def _sub(p: Point, q: Point) -> Point:
    return (p[0] - q[0], p[1] - q[1])


def _cross(u: Point, v: Point) -> float:
    return u[0] * v[1] - u[1] * v[0]


def _dot(u: Point, v: Point) -> float:
    return u[0] * v[0] + u[1] * v[1]


def _unit(u: Point) -> Point:
    n = math.hypot(*u)
    return (u[0] / n, u[1] / n)


def merge_collinear(points: Sequence[Point]) -> tuple[Point, ...]:
    """Drop repeated points and interior vertices where the direction does not change."""
    out: list[Point] = []
    for p in points:
        if out and math.dist(out[-1], p) < _TINY:
            continue
        while len(out) >= 2:
            u, v = _sub(out[-1], out[-2]), _sub(p, out[-1])
            lu, lv = math.hypot(*u), math.hypot(*v)
            if abs(_cross(u, v)) <= 1e-9 * lu * lv and _dot(u, v) > 0:
                out.pop()
            else:
                break
        out.append(p)
    return tuple(out)


def _turn_deg(u: Point, v: Point) -> float:
    return math.degrees(math.atan2(abs(_cross(u, v)), _dot(u, v)))


def _sense(u: Point, v: Point) -> int:
    c = _cross(u, v)
    return 0 if abs(c) < 1e-12 else (1 if c > 0 else -1)


def _piece_rule(same: bool, width: float) -> float:
    return CHAMFER_MIN(width) if same else JOG_MIN(width)


def _move_last_bend(pts: list[Point], target: Point, width: float) -> list[Point] | None:
    """...P, V, G -> ...P, X, target with X on line PV and X->target parallel to V->G."""
    if len(pts) < 3:
        return None
    p, v, g = pts[-3], pts[-2], pts[-1]
    e, d = _unit(_sub(v, p)), _unit(_sub(g, v))
    den = _cross(e, d)
    if abs(den) < 1e-12:
        return None
    s = _cross(_sub(target, p), d) / den
    x = (p[0] + s * e[0], p[1] + s * e[1])
    if s <= _TINY or _dot(_sub(target, x), d) <= _TINY:
        return None
    if len(pts) >= 4:  # P is a bend: the piece P..X must keep its minimum
        same = _sense(_sub(p, pts[-4]), e) == _sense(e, d)
        if s + 1e-9 < _piece_rule(same, width):
            return None
    return [*pts[:-2], x, target]


def _stubs(pts: list[Point], target: Point) -> list[list[Point]]:
    """...G -> ...G, Q, target: one 45° piece and one axis-parallel piece (either order)."""
    g = pts[-1]
    dx, dy = target[0] - g[0], target[1] - g[1]
    m = min(abs(dx), abs(dy))
    diag = (math.copysign(m, dx), math.copysign(m, dy))
    axis = (dx - diag[0], dy - diag[1])
    out = []
    for first in (diag, axis):
        q = (g[0] + first[0], g[1] + first[1])
        out.append(list(merge_collinear([*pts, q, target])))
    return out


def _turns_ok(pts: Sequence[Point]) -> bool:
    return all(
        _turn_deg(_sub(b, a), _sub(c, b)) <= 45.0 + 1e-6
        for a, b, c in zip(pts, pts[1:], pts[2:], strict=False)
    )


def _snap_tail(points: Sequence[Point], target: Point, width: float) -> list[Point]:
    pts = list(merge_collinear(points))
    if math.dist(pts[-1], target) < _TINY:
        return [*pts[:-1], target]
    moved = _move_last_bend(pts, target, width)
    if moved is not None and _turns_ok(moved):
        return moved
    for cand in _stubs(pts, target):
        if _turns_ok(cand):
            return cand
    raise SnapError(f"no octilinear approach from {pts[-1]} to {target}")


def snap_ends(
    points: Sequence[Point], a: Point | None, b: Point | None, width: float
) -> tuple[Point, ...]:
    """`points` with its first point replaced by the real pad centre `a` and its last by `b`
    (None = keep that end, e.g. a via), every segment octilinear; rounded to 1 nm.
    Raises `SnapError` when no approach keeps every direction change at ≤ 45°."""
    pts = list(merge_collinear(points))
    if b is not None:
        pts = _snap_tail(pts, b, width)
    if a is not None:
        pts = _snap_tail(pts[::-1], a, width)[::-1]
    return merge_collinear([(round(x, _ROUND), round(y, _ROUND)) for x, y in pts])


def to_path(
    states: Sequence[tuple[int, int, int]], grid: ObstacleGrid, req: RouteRequest
) -> RoutePath:
    """`states` = the searched nodes (layer index, i, j) from pad `a` to pad `b`: per-layer
    runs (split at layer changes = vias), collinear runs merged, the ends snapped to the
    exact pad centres. Raises `SnapError` like `snap_ends`."""
    runs: list[tuple[int, list[Point]]] = []
    vias: list[Point] = []
    for li, i, j in states:
        p = grid.xy(i, j)
        if runs and runs[-1][0] != li:
            vias.append((round(p[0], _ROUND), round(p[1], _ROUND)))
            runs.append((li, [p]))
        elif runs:
            runs[-1][1].append(p)
        else:
            runs.append((li, [p]))
    segments: list[tuple[str, Point, Point]] = []
    length = 0.0
    last = len(runs) - 1
    for k, (li, pts) in enumerate(runs):
        a = (req.a.x, req.a.y) if k == 0 else None
        b = (req.b.x, req.b.y) if k == last else None
        if len(pts) == 1 and a is None and b is None:
            continue
        snapped = snap_ends(pts, a, b, req.width)
        for p, q in itertools.pairwise(snapped):
            segments.append((grid.layers[li], p, q))
            length += math.dist(p, q)
    return RoutePath(segments=tuple(segments), vias=tuple(vias), length=length)


def layer_runs(path: RoutePath) -> list[tuple[str, tuple[Point, ...]]]:
    """The path as per-layer polylines (a new run at every layer change)."""
    runs: list[tuple[str, list[Point]]] = []
    for layer, p, q in path.segments:
        if runs and runs[-1][0] == layer and runs[-1][1][-1] == p:
            runs[-1][1].append(q)
        else:
            runs.append((layer, [p, q]))
    return [(layer, tuple(pts)) for layer, pts in runs]
