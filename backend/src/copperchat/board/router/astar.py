"""Octilinear grid A* (B-C12, P5a Task 2).

State = (node, layer, direction, sense of the last 45° bend, straight steps since it). The
only moves are: one step straight on, one step after a ±45° turn, and a via to another layer
at the same node. A turn is allowed when there is no earlier bend to respect (start, after a
via, or the straight piece since the last bend already covers both minimums), or when the
straight piece since the last bend covers `CHAMFER_MIN(width)` (same sense: two halves of a
90°) or `JOG_MIN(width)` (opposite sense: an S-offset). Turns of 90° or more at one vertex
are not moves at all, so the search cannot produce them.

The first move from pad `a` and the last move into pad `b` follow the pad's long axis or run
at 45° to it (square and round pads: any direction). A goal state is accepted only when the
snapped path (`simplify.to_path`) passes `analyse_bends` (B-C5) on every layer run and the
exact clearance check (`ObstacleGrid.segment_clear`); otherwise the search goes on.

Round 2 (R3, owner rule a): a branch off existing own-net copper is a T - it meets a
straight own track perpendicularly - never a Y (45° / 135°). With `req.tree` the goal is pad
`b` or a node next to the interior of an own track connected to `b`, entered by a move
perpendicular to that track (the path's end then lands exactly on the track). A bend
inside an end pad's entry zone (`geom.entry_zone`, outside the pad) that closely follows
another bend costs ENTRY_TURN_COST more, and a path whose pad entry loops or jogs (M16 b /
c, `geom.analyse_entry`; both errors for the owner) is accepted only as `solve`'s last
resort.
"""

from __future__ import annotations

import heapq
import itertools
import math
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import replace
from typing import Any

from copperchat.board.geom import (
    CHAMFER_MIN,
    JOG_MIN,
    _in_pad,
    _pad_on_layer,
    analyse_bends,
    analyse_entry,
    entry_zone,
    junction_kind,
    point_in_polygon,
)
from copperchat.board.model import BoardModel, Pad, Point
from copperchat.board.router.grid import (
    BLOCKED,
    WINDOW_MM,
    DeadlineExpired,
    ObstacleGrid,
    RoutePath,
    RouteRequest,
    _shape_bbox,
    build_grid,
    check_deadline,
    path_clear,
    seg_shape,
)
from copperchat.board.router.simplify import SnapError, layer_runs, merge_collinear, to_path

TURN_COST = 1.0  # mm-equivalent per 45° bend (0.3 planned; 1.0 measured: fewer bends, same speed)
# R3 (owner rule c): extra cost of a bend inside an end pad's entry zone (outside the pad)
# that follows another bend within the straight-piece minimums (a jog or a loop forming); a
# single 45° bend next to a pad is free (charging it, sp4_synth's decoupling paths detoured)
ENTRY_TURN_COST = 4.0
VIA_COST = 2.0  # mm-equivalent per via
LAYER_FACTOR = 1.2  # step cost on every layer but the request's first
PAIR_BAND_FACTOR = 0.8  # step cost inside a grid's `cheap` band (pair fallback)
# R4: step cost factor inside another net's unrouted pad's entry zone (`grid.escape`)
ESCAPE_COST_FACTOR = 1.5
MAX_GOAL_REJECTS = 64  # goal states whose snapped path failed the final check
# f = g + HEURISTIC_WEIGHT * h (weighted A*): a detour around an obstacle otherwise expands
# the whole ellipse of nodes with g + h below the detour's cost (> 1M states for a 4 mm
# detour at 0.05 mm). Paths stay within this factor of the cheapest.
HEURISTIC_WEIGHT = 1.5

# board coordinates, y down; k = 0..7 counter-clockwise on screen from +x
DIRS: tuple[tuple[int, int], ...] = (
    (1, 0),
    (1, -1),
    (0, -1),
    (-1, -1),
    (-1, 0),
    (-1, 1),
    (0, 1),
    (1, 1),
)
_VIA_D = 8  # direction "none" after a via
_START_D = 9  # direction "none" at the start pad
_ND = 10
# R4 (owner 2026-11-27, M16 b / c hard): the bends a path made in an end pad's entry zone
# since it last was outside one - 0 none, 1 / 2 one bend of sense +45° / -45°, 3 / 4 two of
# that sense (90°). _ZONE_NEXT[z][sense] = the state after one more bend there, -1 = refused:
# the opposite sense is a jog, a third bend a loop (cumulative 135°).
_NZ = 5
_ZONE_NEXT = ((0, 1, 2), (0, 3, -1), (0, -1, 4), (0, -1, -1), (0, -1, -1))
_EPS = 1e-9


PAD_AXIS_RATIO = 1.5  # pads less elongated than this have no long axis (R3)


def pad_dirs(pad: Pad) -> frozenset[int]:
    """Directions a track may leave (or enter) `pad` in: the long axis both ways and ±45°
    to it — 6 of 8; round pads and pads with long / short side < PAD_AXIS_RATIO (R3: an
    0603 pad of 0.8 x 0.95 mm is left straight towards its neighbour, owner rule b): all 8.
    The axis is the pad's rotation rounded to the nearest 45°."""
    lo, hi = sorted((pad.w, pad.h))
    if pad.shape == "circle" or hi < PAD_AXIS_RATIO * lo:
        return frozenset(range(8))
    k = round((pad.rot % 360.0) / 45.0) % 8
    if pad.h > pad.w:
        k = (k + 2) % 8
    return frozenset((k + o) % 8 for o in (-1, 0, 1, 3, 4, 5))


def _goal_node(grid: ObstacleGrid, pad: Pad) -> tuple[int, int]:
    """The node nearest the pad centre that lies inside the pad (nearest overall if none)."""
    ci, cj = grid.cell(pad.x, pad.y)
    best: tuple[float, int, int] | None = None
    for i in range(ci - 1, ci + 2):
        for j in range(cj - 1, cj + 2):
            if not grid.inside(i, j):
                continue
            p = grid.xy(i, j)
            if not _in_pad(p, pad):
                continue
            d = math.dist(p, (pad.x, pad.y))
            if best is None or d < best[0] - _EPS:
                best = (d, i, j)
    return (best[1], best[2]) if best else (ci, cj)


def _dir_index(p: Point, q: Point) -> int:
    ang = math.degrees(math.atan2(-(q[1] - p[1]), q[0] - p[0])) % 360.0
    return round(ang / 45.0) % 8


def _crossing_dir(pts: Sequence[Point], pad: Pad) -> int | None:
    """Direction of the segment on which `pts` (starting in `pad`) leaves the pad."""
    for p, q in itertools.pairwise(pts):
        if not _in_pad(q, pad):
            return _dir_index(p, q)
    return None


def pad_exits_ok(path: RoutePath, req: RouteRequest) -> bool:
    """The segment that leaves pad `a` and the one that enters pad `b` run along the pad's
    long axis or at 45° to it (`pad_dirs`, either way)."""
    runs = layer_runs(path)
    if not runs:
        return False
    first = _crossing_dir(runs[0][1], req.a)
    tail = runs[-1][1]
    if tree_end(path, req):  # a T onto own copper: no pad b to enter
        return first is None or first in pad_dirs(req.a)
    last = _crossing_dir(tail[::-1], req.b)
    return (first is None or first in pad_dirs(req.a)) and (last is None or last in pad_dirs(req.b))


def tree_end(path: RoutePath, req: RouteRequest) -> bool:
    """The path of a tree request ends on own copper (a T), not in pad `b`."""
    return req.tree and bool(path.segments) and not _in_pad(path.segments[-1][2], req.b)


JUNCTION_TOL = 0.002  # mm: a point this close to own copper touches it (geom._JOIN_TOL)
_RIGHT_TOL_DEG = 0.5  # M4's angle tolerance (geom.ANGLE_TOL_DEG)
_MASK_DEADLINE_EVERY = 1024  # samples between deadline checks in junction_masks


def _seg_dist(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = dx * dx + dy * dy
    t = 0.0 if n == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / n))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def _cross_point(a: Point, b: Point, c: Point, d: Point) -> Point | None:
    """The proper crossing point of segments ab and cd (None when parallel or apart)."""
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    den = rx * sy - ry * sx
    if abs(den) < 1e-12:
        return None
    t = ((c[0] - a[0]) * sy - (c[1] - a[1]) * sx) / den
    u = ((c[0] - a[0]) * ry - (c[1] - a[1]) * rx) / den
    if 0.0 < t < 1.0 and 0.0 < u < 1.0:
        return (a[0] + t * rx, a[1] + t * ry)
    return None


def _heads(node: Point, segs: Iterable[tuple[Point, Point]]) -> list[float]:
    """Headings (degrees) from `node` along every segment touching it."""
    out = []
    for a, b in segs:
        if _seg_dist(node, a, b) > JUNCTION_TOL:
            continue
        for end in (a, b):
            if math.dist(node, end) > JUNCTION_TOL:
                out.append(math.degrees(math.atan2(end[1] - node[1], end[0] - node[0])))
    return out


def _angle_diff(h1: float, h2: float) -> float:
    """|h1 - h2| folded into [0, 180] degrees."""
    return abs((h1 - h2 + 180.0) % 360.0 - 180.0)


def _distinct(headings: Iterable[float]) -> list[float]:
    """The headings with duplicates (within the angle tolerance) dropped: a path sharing an
    own track collinearly adds no new direction."""
    out: list[float] = []
    for h in headings:
        if not any(_angle_diff(h, o) <= _RIGHT_TOL_DEG for o in out):
            out.append(h)
    return out


def junctions_ok(
    runs: Sequence[tuple[str, Sequence[Point]]],
    own: Mapping[str, Sequence[tuple[Point, Point]]],
    exempt: Callable[[str, Point], bool],
    pad_at: Callable[[str, Point], bool] | None = None,
) -> bool:
    """Where the path touches EXISTING own-net tracks of its layer outside `exempt(layer,
    point)` (own pads of the layer, vias) - a path vertex on them, an own track end on the
    path, a proper crossing - the distinct headings of both meeting there form a T
    (`geom.junction_kind` "t", owner rule a) or, with two headings, a straight run or a 45°
    bend. A Y (45° / 135° branch) is refused, and so is an X (the router never crosses own
    copper, round-2 plan call 3) and a 90° corner made of path and own track."""
    for layer, pts in runs:
        segs = own.get(layer) or ()
        if not segs:
            continue
        mine = list(itertools.pairwise(pts))
        nodes = list(pts)
        for a, b in mine:
            for c, d in segs:
                x = _cross_point(a, b, c, d)
                if x is not None:
                    nodes.append(x)
                for end in (c, d):  # an own track ending on the path's interior
                    if _seg_dist(end, a, b) <= JUNCTION_TOL:
                        nodes.append(end)
        for node in nodes:
            old = _heads(node, segs)
            if not old:
                continue
            hs = _distinct([*old, *_heads(node, mine)])
            if exempt(layer, node):
                # a joint at a pad or via (no T / Y check there); but at a via two tracks
                # meeting at 90 degrees are still a 90-degree corner (M4 counts it)
                if len(hs) == 2 and pad_at is not None and not pad_at(layer, node):
                    diff = _angle_diff(hs[0], hs[1])
                    if abs(diff - 90.0) <= _RIGHT_TOL_DEG:
                        return False
                continue
            if len(hs) >= 3:
                if junction_kind(hs) != "t":
                    return False
            elif len(hs) == 2:
                diff = _angle_diff(hs[0], hs[1])
                if not (abs(diff - 180.0) <= _RIGHT_TOL_DEG or abs(diff - 135.0) <= _RIGHT_TOL_DEG):
                    return False
    return True


def _step_hit(p: Point, q: Point, a: Point, b: Point) -> Point | None:
    """Where the lattice step pq meets segment ab (a proper crossing, or an end of either
    within JUNCTION_TOL of the other), else None."""
    x = _cross_point(p, q, a, b)
    if x is not None:
        return x
    for pt in (p, q):
        if _seg_dist(pt, a, b) <= JUNCTION_TOL:
            return pt
    for pt in (a, b):
        if _seg_dist(pt, p, q) <= JUNCTION_TOL:
            return pt
    return None


def junction_masks(grid: ObstacleGrid, deadline: float | None = None) -> list[dict[int, int]]:
    """Per layer: lattice node -> bitmask of the move directions (DIRS indices) the search
    may not take there, because of existing own-net copper (owner rule a, round 2): at a
    node outside own pads of the layer and vias, a move at 45° or 135° to an own track that
    it touches or crosses is blocked (a Y); a move perpendicular to it is allowed when it
    ARRIVES on the track (a T) and blocked when it leaves the track sideways or crosses it
    (an X or a 90° corner). Moves along the track are free. `junctions_ok` still checks the
    snapped path. Computed once per grid (cached); each segment is clipped to the window and
    checked against only the spots near it; `DeadlineExpired` past `deadline`."""
    if grid._masks is not None:
        return grid._masks
    out: list[dict[int, int]] = [{} for _ in grid.layers]
    w, pitch = grid.w, grid.pitch
    wx0, wy0 = grid.x0 - pitch, grid.y0 - pitch
    wx1, wy1 = grid.x0 + grid.w * pitch, grid.y0 + grid.h * pitch
    samples = 0
    for li, segs in enumerate(grid.own_segments):
        masks = out[li]
        spots = grid.own_spots[li] if grid.own_spots else ()
        # the layer's exempt spots bucketed by their bboxes (1 mm cells), bbox precomputed
        buckets: dict[tuple[int, int], list[tuple[tuple[float, float, float, float], Any]]] = {}
        for sp in spots:
            bb = _shape_bbox(sp)
            for bj in range(math.floor(bb[1]), math.floor(bb[3]) + 1):
                for bi in range(math.floor(bb[0]), math.floor(bb[2]) + 1):
                    buckets.setdefault((bi, bj), []).append((bb, sp))

        def exempt(pt: Point, buckets: Mapping[Any, Any] = buckets) -> bool:
            for bb, sp in buckets.get((math.floor(pt[0]), math.floor(pt[1])), ()):
                inside = (
                    bb[0] - 1e-9 <= pt[0] <= bb[2] + 1e-9 and bb[1] - 1e-9 <= pt[1] <= bb[3] + 1e-9
                )
                if inside and seg_shape(pt, pt, sp) <= 1e-9:
                    return True
            return False

        def block(n: int, d: int, both: bool, m: int, masks: dict[int, int] = masks) -> None:
            masks[n] = masks.get(n, 0) | (1 << d)
            if both:
                masks[m] = masks.get(m, 0) | (1 << ((d + 4) & 7))

        for a, b in segs:
            length = math.dist(a, b)
            if length < JUNCTION_TOL:
                continue
            ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
            if abs(ang / 45.0 - round(ang / 45.0)) * 45.0 > _RIGHT_TOL_DEG:
                continue  # not octilinear: never parallel to a lattice move
            # clip the segment to the window (parametric, both axes)
            t0, t1 = 0.0, 1.0
            for p0, d, lo, hi in ((a[0], b[0] - a[0], wx0, wx1), (a[1], b[1] - a[1], wy0, wy1)):
                if abs(d) < 1e-12:
                    if not lo <= p0 <= hi:
                        t0, t1 = 1.0, 0.0
                    continue
                u0, u1 = sorted(((lo - p0) / d, (hi - p0) / d))
                t0, t1 = max(t0, u0), min(t1, u1)
            if t0 > t1:
                continue
            h = _dir_index(a, b)
            # one sample per pitch: the 3x3 nodes around each sample cover every node next to
            # the segment
            steps = max(1, math.ceil(length * (t1 - t0) / pitch))
            perp = ((h + 2) & 7, (h + 6) & 7)
            slant = ((h + 1) & 7, (h + 3) & 7, (h + 5) & 7, (h + 7) & 7)
            seen: set[int] = set()
            for k in range(steps + 1):
                samples += 1
                if not samples % _MASK_DEADLINE_EVERY:
                    check_deadline(deadline)
                t = t0 + (t1 - t0) * k / steps
                ci, cj = grid.cell(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
                # every lattice step near the segment (the segment need not lie on the
                # request's lattice, P9a round-2 review)
                for j in range(cj - 1, cj + 2):
                    for i in range(ci - 1, ci + 2):
                        if not grid.inside(i, j) or (n := j * w + i) in seen:
                            continue
                        seen.add(n)
                        p = grid.xy(i, j)
                        p_on = _seg_dist(p, a, b) <= JUNCTION_TOL
                        for d in perp:
                            di, dj = DIRS[d]
                            if not grid.inside(i + di, j + dj):
                                continue
                            q = grid.xy(i + di, j + dj)
                            m = n + di + dj * w
                            if p_on:  # leaving the track sideways: an X or a 90° corner
                                if not exempt(p):
                                    block(n, d, False, m)
                                continue
                            if _seg_dist(q, a, b) <= JUNCTION_TOL:
                                continue  # arriving on the track: a T
                            x = _cross_point(p, q, a, b)
                            if x is not None and not exempt(x):
                                block(n, d, True, m)  # straight across: an X
                        for d in slant:
                            di, dj = DIRS[d]
                            if not grid.inside(i + di, j + dj):
                                continue
                            q = grid.xy(i + di, j + dj)
                            hit = _step_hit(p, q, a, b)
                            if hit is None or exempt(hit):
                                continue
                            block(n, d, True, n + di + dj * w)  # a Y either way
    grid._masks = out
    return out


def _entry_bad(pts: Sequence[Point], pad: Pad, width: float) -> bool:
    """The run starting in `pad` turns back (M16 pad loop) or makes an S-offset (M16 pad
    jog) inside the pad's entry zone - both errors for the owner (2026-11-27)."""
    if not pts or not _in_pad(pts[0], pad):
        return False
    rep = analyse_entry(pts, width, entry_zone(pad, width), in_pad=lambda q: _in_pad(q, pad))
    return bool(rep.loops or rep.jogs)


def entry_nodes(grid: ObstacleGrid, pads: Sequence[Pad], width: float) -> set[int]:
    """The lattice nodes inside the entry zone of one of `pads` but outside that pad."""
    out: set[int] = set()
    w = grid.w
    for pad in pads:
        zone = list(entry_zone(pad, width))
        xs, ys = [q[0] for q in zone], [q[1] for q in zone]
        i0, j0 = grid.cell(min(xs), min(ys))
        i1, j1 = grid.cell(max(xs), max(ys))
        for j in range(max(0, j0), min(grid.h - 1, j1) + 1):
            for i in range(max(0, i0), min(w - 1, i1) + 1):
                q = grid.xy(i, j)
                if point_in_polygon(q, zone) and not _in_pad(q, pad):
                    out.add(j * w + i)
    return out


def valid_path(
    path: RoutePath, grid: ObstacleGrid, req: RouteRequest, *, entry_ok: bool = False
) -> bool:
    """The path starts on a layer of pad `a` and ends on one of pad `b`; `analyse_bends` has
    no failure on any layer run (the end-bend exemption only for a bend inside its pad —
    stricter than the board metric, which exempts every pad-end bend); every segment and
    via keeps its exact clearance, also between the path's own vias. A tree request's path
    may instead end ON an own track connected to pad `b` (`tree_end`, R3). A pad entry that
    loops or jogs (M16 b / c) fails unless `entry_ok` (`solve`'s last resort)."""
    runs = layer_runs(path)
    if not runs:
        return False
    t_end = tree_end(path, req)
    if t_end:
        li = grid.layers.index(runs[-1][0]) if runs[-1][0] in grid.layers else -1
        end = runs[-1][1][-1]
        segs = grid.tree_segments[li] if 0 <= li < len(grid.tree_segments) else ()
        if not any(_seg_dist(end, c, d) <= JUNCTION_TOL for c, d in segs):
            return False
    if not (_pad_on_layer(req.a, runs[0][0]) and (t_end or _pad_on_layer(req.b, runs[-1][0]))):
        return False
    last = len(runs) - 1
    for k, (_layer, raw) in enumerate(runs):
        pts = merge_collinear(raw)
        # the pad-entry exemption only for an end bend that lies inside its pad
        ends = (
            k == 0 and len(pts) >= 3 and _in_pad(pts[1], req.a),
            k == last and not t_end and len(pts) >= 3 and _in_pad(pts[-2], req.b),
        )
        if not entry_ok and k == 0 and _entry_bad(pts, req.a, req.width):
            return False
        if not entry_ok and k == last and not t_end and _entry_bad(pts[::-1], req.b, req.width):
            return False
        r = analyse_bends(pts, req.width, pad_ends=ends)
        if (
            r.non_octilinear
            or r.ninety
            or r.acute
            or r.off_angle
            or r.micro_jogs
            or r.short_chamfers
        ):
            return False
    # two vias of one path keep copper clearance between them (P5b: a via pair 0.05 mm
    # apart faked a 135° turn; KiCad's DRC rejected the overlapping holes)
    gap = req.via[0] + req.clearance - 1e-6
    if any(math.dist(p, q) < gap for p, q in itertools.combinations(path.vias, 2)):
        return False
    if grid.own_segments:
        own = dict(zip(grid.layers, grid.own_segments, strict=True))
        index = {name: k for k, name in enumerate(grid.layers)}
        mine = list(path.vias)

        def exempt(layer: str, p: Point) -> bool:
            # the path's own new vias are joints too (M4 exempts nodes at vias)
            if any(math.dist(p, v) <= req.via[0] / 2 for v in mine):
                return True
            return grid.own_exempt(index[layer], p)

        def pad_at(layer: str, p: Point) -> bool:
            return grid.own_pad_at(index[layer], p)

        runs_m = [(lay, merge_collinear(raw)) for lay, raw in runs]
        if not junctions_ok(runs_m, own, exempt, pad_at):
            return False
    return path_clear(grid, path)


_FREE_RUN = re.compile(rb"[\x00\x01]+")
_VIA_RUN = re.compile(rb"\x00+")


def _pairs(
    a: list[tuple[int, int, int]], b: list[tuple[int, int, int]]
) -> Iterator[tuple[int, int, int, int, int, int]]:
    """Overlapping pairs of two sorted lists of disjoint intervals (two-pointer sweep):
    (id a, id b, a0, a1, b0, b1)."""
    i = k = 0
    while i < len(a) and k < len(b):
        a0, a1, ra = a[i]
        b0, b1, rb = b[k]
        if a0 <= b1 and b0 <= a1:
            yield ra, rb, a0, a1, b0, b1
        if a1 < b1:
            i += 1
        else:
            k += 1


def _overlaps(
    a: list[tuple[int, int, int]], b: list[tuple[int, int, int]], slack: int
) -> Iterator[tuple[int, int]]:
    """Ids of the overlapping pairs of two sorted disjoint interval lists, intervals grown
    by `slack` (linear, unlike the all-pairs loop)."""
    grown = [(b0 - slack, b1 + slack, rb) for b0, b1, rb in b]
    for ra, rb, *_ in _pairs(a, grown):
        yield ra, rb


def reachable(
    grid: ObstacleGrid,
    start_n: int,
    goal_n: int,
    a_layers: list[int],
    b_layers: frozenset[int],
    vias: bool,
    *,
    deadline: float | None = None,
) -> bool:
    """Cheap relaxation of the search: are the start and goal nodes in one 8-connected
    component of free nodes (layers joined at via-capable nodes)? No → no path exists.
    Runs of free nodes per row are joined by a union-find."""
    w, h, nl = grid.w, grid.h, len(grid.layers)
    parent: list[int] = []

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[max(rx, ry)] = min(rx, ry)

    forced = {start_n, goal_n}
    runs: list[list[list[tuple[int, int, int]]]] = []  # [layer][row] -> (i0, i1, id)
    for li in range(nl):
        cells = grid.cells[li]
        rows: list[list[tuple[int, int, int]]] = []
        for j in range(h):
            if not j % 64:
                check_deadline(deadline)
            row = bytearray(cells[j * w : (j + 1) * w])
            for n in forced:
                if n // w == j:
                    row[n % w] = 0
            here: list[tuple[int, int, int]] = []
            for mt in _FREE_RUN.finditer(row):
                rid = len(parent)
                parent.append(rid)
                i0, i1 = mt.start(), mt.end() - 1
                here.append((i0, i1, rid))
            if rows:  # 8-connected: runs overlapping with one node of slack
                for x, y in _overlaps(here, rows[-1], 1):
                    union(x, y)
            rows.append(here)
        runs.append(rows)
    if vias and nl > 1:
        for j in range(h):
            vruns = [
                (mt.start() - j * w, mt.end() - 1 - j * w, -1)
                for mt in _VIA_RUN.finditer(grid.via_blocked, j * w, (j + 1) * w)
            ]
            if not vruns:
                continue
            # per layer the parts of its free runs where a via fits; parts of different
            # layers that overlap share a node free on both -> one sweep over all of them
            capable = sorted(
                (max(a0, v0), min(a1, v1), ra)
                for li in range(nl)
                for ra, _v, a0, a1, v0, v1 in _pairs(runs[li][j], vruns)
            )
            reach_end, reach_id = -1, -1
            for c0, c1, cid in capable:
                if c0 <= reach_end:
                    union(cid, reach_id)
                if c1 > reach_end:
                    reach_end, reach_id = c1, cid

    def comp(n: int, layers: Iterable[int]) -> set[int]:
        j, i = n // w, n % w
        return {find(rid) for li in layers for i0, i1, rid in runs[li][j] if i0 <= i <= i1}

    return bool(comp(start_n, a_layers) & comp(goal_n, b_layers))


USE_COARSE = True
COARSE_MM = 0.5  # cell of the coarse distance field behind the heuristic


_FREE_TABLE = bytes(0 if v == BLOCKED else 1 for v in range(256))


def _band_edges(
    x_same: bytes, x_next: bytes, x_prev: bytes, cf: int, nb: int
) -> Iterator[tuple[int, int]]:
    """Coarse links across one boundary: `x_same[t]` = a fine move from position t on the
    near side to t on the far side, `x_next[t]` to t + 1, `x_prev[t]` to t - 1 (bytes 0/1).
    Yields (near band, far band) pairs (bands of `cf` positions)."""
    for b in range(nb):
        lo, hi = b * cf, min((b + 1) * cf, len(x_same))
        if 1 in x_same[lo:hi] or 1 in x_next[lo : hi - 1] or 1 in x_prev[lo + 1 : hi]:
            yield b, b
        if hi < len(x_same) and x_next[hi - 1]:
            yield b, b + 1
        if lo > 0 and x_prev[lo]:
            yield b, b - 1


def _links(a: bytes, b: bytes) -> tuple[bytes, bytes, bytes]:
    n = len(a)
    ia, ib = int.from_bytes(a, "big"), int.from_bytes(b, "big")
    same = (ia & ib).to_bytes(n, "big")
    nxt = (ia & (ib << 8)).to_bytes(n + 1, "big")[1:]  # a[t] & b[t + 1]
    prv = (ia & (ib >> 8)).to_bytes(n, "big")  # a[t] & b[t - 1]
    return same, nxt, prv


def coarse_distances(
    grid: ObstacleGrid, gi: int, gj: int, forced: Iterable[int], seeds: Iterable[int] = ()
) -> tuple[int, int, list[float]]:
    """(factor, coarse width, distance to the goal per coarse cell). Coarse cells are
    COARSE_MM squares; two neighbouring cells are linked when a fine move (on any layer)
    joins a free node of one to a free node of the other, and a diagonal neighbour also
    through either side cell. Every real path maps to a linked cell sequence, so
    `distance - slack` (two cell diagonals) stays a near lower bound; cells the goal cannot
    be reached from are `inf`. `seeds`: more goal nodes (tree goals, R3), distance 0 too."""
    w, h = grid.w, grid.h
    cf = max(1, round(COARSE_MM / grid.pitch))
    cw, ch = -(-w // cf), -(-h // cf)
    forced = list(forced)
    # free masks of the boundary columns / rows, OR over the layers
    cols: dict[int, bytearray] = {}
    rows: dict[int, bytearray] = {}
    for ci in range(1, cw):
        for i in (ci * cf - 1, ci * cf):
            cols[i] = bytearray(h)
    for cj in range(1, ch):
        for j in (cj * cf - 1, cj * cf):
            rows[j] = bytearray(w)
    for lay in grid.cells:
        for i, acc in cols.items():
            col = lay[i::w].translate(_FREE_TABLE)
            acc[:] = (int.from_bytes(acc, "big") | int.from_bytes(col, "big")).to_bytes(h, "big")
        for j, acc in rows.items():
            row = lay[j * w : (j + 1) * w].translate(_FREE_TABLE)
            acc[:] = (int.from_bytes(acc, "big") | int.from_bytes(row, "big")).to_bytes(w, "big")
    for n in forced:
        i, j = n % w, n // w
        if i in cols:
            cols[i][j] = 1
        if j in rows:
            rows[j][i] = 1
    adj: list[set[int]] = [set() for _ in range(cw * ch)]
    for ci in range(1, cw):
        same, nxt, prv = _links(bytes(cols[ci * cf - 1]), bytes(cols[ci * cf]))
        for b0, b1 in _band_edges(same, nxt, prv, cf, ch):
            c0, c1 = b0 * cw + ci - 1, b1 * cw + ci
            adj[c0].add(c1)
            adj[c1].add(c0)
    for cj in range(1, ch):
        same, nxt, prv = _links(bytes(rows[cj * cf - 1]), bytes(rows[cj * cf]))
        for b0, b1 in _band_edges(same, nxt, prv, cf, cw):
            c0, c1 = (cj - 1) * cw + b0, cj * cw + b1
            adj[c0].add(c1)
            adj[c1].add(c0)
    dist = [math.inf] * (cw * ch)
    goals = {(gj // cf) * cw + gi // cf} | {((n // w) // cf) * cw + (n % w) // cf for n in seeds}
    heap = []
    for goal in goals:
        dist[goal] = 0.0
        heap.append((0.0, goal))
    cs = cf * grid.pitch
    cd = cs * math.sqrt(2.0)
    while heap:
        d, c = heapq.heappop(heap)
        if d > dist[c]:
            continue
        links = adj[c]
        ci, cj = c % cw, c // cw
        for m in links:
            mi, mj = m % cw, m // cw
            nd = d + (cd if mi != ci and mj != cj else cs)
            if nd < dist[m]:
                dist[m] = nd
                heapq.heappush(heap, (nd, m))
        for di in (-1, 1):  # diagonal through a side cell
            for dj in (-1, 1):
                mi, mj = ci + di, cj + dj
                if not (0 <= mi < cw and 0 <= mj < ch):
                    continue
                m = mj * cw + mi
                s1, s2 = cj * cw + mi, mj * cw + ci
                if (s1 in links and m in adj[s1]) or (s2 in links and m in adj[s2]):
                    nd = d + cd
                    if nd < dist[m]:
                        dist[m] = nd
                        heapq.heappush(heap, (nd, m))
    return cf, cw, dist


def route(
    grid: ObstacleGrid,
    req: RouteRequest,
    *,
    max_expansions: int = 2_000_000,
    deadline: float | None = None,
    entry_ok: bool = False,
) -> RoutePath | None:
    """The cheapest octilinear path from `req.a` to `req.b` on `grid` that passes the final
    checks, or None (no path in the window, `max_expansions` exhausted, or too many goal
    states rejected). One search (after two cheap feasibility checks); never rebuilds or
    widens the grid. A tree request (R3) searches for pad b or a T onto its copper
    (`tree_goals`); when that finds nothing, a second search for pad b alone follows."""
    nl = len(grid.layers)
    a_layers = [k for k, name in enumerate(grid.layers) if _pad_on_layer(req.a, name)]
    b_layers = [k for k, name in enumerate(grid.layers) if _pad_on_layer(req.b, name)]
    if not a_layers or not b_layers:
        return None
    si, sj = grid.cell(req.a.x, req.a.y)
    gi, gj = _goal_node(grid, req.b)
    if not (grid.inside(si, sj) and grid.inside(gi, gj)):
        return None
    w = grid.w
    start_n, goal_n = sj * w + si, gj * w + gi
    try:
        ok = reachable(
            grid,
            start_n,
            goal_n,
            a_layers,
            frozenset(b_layers),
            req.allow_vias and nl > 1,
            deadline=deadline,
        )
    except DeadlineExpired:
        return None
    if not ok:
        return None
    goal_dirs = pad_dirs(req.b)
    goal_layers = frozenset(b_layers)
    if start_n == goal_n:  # both pads at one node: the direct (snapped) connection
        for li in a_layers:
            if li in goal_layers:
                try:
                    direct = to_path([(li, si, sj)], grid, req)
                except SnapError:
                    continue
                if direct.segments and valid_path(direct, grid, req, entry_ok=entry_ok):
                    return direct
        if goal_layers & set(a_layers):
            return None
        # stacked pads on different layers: search (a via may join them)

    tg = tree_goals(grid, req) if req.tree else []
    extra = [n for layer in tg for n in layer]

    def at_goal(n: int, li: int, d: int) -> bool:
        if n == goal_n and li in goal_layers and d in goal_dirs:
            return True
        hit = tg[li].get(n) if tg else None
        return hit is not None and bool((hit[0] >> d) & 1)

    def end_of(n: int, li: int, d: int) -> Point | None:
        if n == goal_n and li in goal_layers and d in goal_dirs:
            return None  # pad b
        return tg[li][n][1]

    found = _search(
        grid,
        req,
        req.a,
        start_n,
        a_layers,
        goal_n,
        at_goal,
        (gi, gj),
        max_expansions,
        deadline,
        extra_goals=extra,
        entry_ok=entry_ok,
        goal_pieces=_goal_pieces(grid, tg),
        end_of=end_of if extra else None,
    )
    if found is None and extra and not (deadline is not None and time.monotonic() > deadline):
        # the T goals are entered only perpendicularly: when that search runs out, the plain
        # connection to pad b gets its own budget (never worse than without `tree`)
        found = _search(
            grid,
            req,
            req.a,
            start_n,
            a_layers,
            goal_n,
            lambda n, li, d: n == goal_n and li in goal_layers and d in goal_dirs,
            (gi, gj),
            max_expansions,
            deadline,
            entry_ok=entry_ok,
        )
    return found


def _goal_pieces(
    grid: ObstacleGrid, tg: Sequence[Mapping[int, tuple[int, Point]]]
) -> list[tuple[Point, Point]]:
    """Per tree segment, the piece between its first and last landing point."""
    out: list[tuple[Point, Point]] = []
    for li, goals in enumerate(tg):
        if not goals:
            continue
        lands = [land for _bits, land in goals.values()]
        for a, b in grid.tree_segments[li]:
            on = [p for p in lands if _seg_dist(p, a, b) <= JUNCTION_TOL]
            if not on:
                continue
            length = math.dist(a, b)
            ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
            ts = [(p[0] - a[0]) * ux + (p[1] - a[1]) * uy for p in on]
            lo, hi = min(ts), max(ts)
            out.append(((a[0] + ux * lo, a[1] + uy * lo), (a[0] + ux * hi, a[1] + uy * hi)))
    return out


TREE_GOALS_MAX = 2_000  # tree goal nodes kept (nearest to pad a)


def tree_goals(grid: ObstacleGrid, req: RouteRequest) -> list[dict[int, tuple[int, Point]]]:
    """Per layer: node -> (bitmask of the arrival directions, landing point) of the T
    goals of a tree request (R3, owner rule a): nodes within one pitch (on their arrival
    side, so an off-lattice track gets one on each side) of the INTERIOR of
    a straight own track connected to pad b (`grid.tree_segments`) - at least `req.width`
    from its ends and from every point where other own copper touches or crosses it (so
    never at a bend vertex, a junction, a free end), never in an own pad or via - entered
    perpendicularly towards the track; the path then ends on the track at the landing point
    (the node projected onto the track). At most TREE_GOALS_MAX, nearest to pad a first."""
    out: list[dict[int, tuple[int, Point]]] = [{} for _ in grid.layers]
    if not grid.tree_segments:
        return out
    w, pitch = grid.w, grid.pitch
    # one node on EACH side of a track that is off the request's lattice (a node at up
    # to one pitch on the arrival side; the extension onto the track stays < 1 pitch)
    reach = pitch - 1e-9
    keep = req.width
    # R4: never land in an own pad's entry zone - the joined path would turn there (a T is
    # 90°), and one more bend of the new branch inside the zone makes an M16 loop or jog
    # measured from that pad
    zones = [entry_zone(q, req.width) for q in grid.own_pads]
    zone_boxes = [
        (min(x for x, _ in z), min(y for _, y in z), max(x for x, _ in z), max(y for _, y in z))
        for z in zones
    ]

    def in_zone(p: Point) -> bool:
        return any(
            b[0] - 1e-9 <= p[0] <= b[2] + 1e-9
            and b[1] - 1e-9 <= p[1] <= b[3] + 1e-9
            and point_in_polygon(p, z)
            for z, b in zip(zones, zone_boxes, strict=True)
        )

    cands: list[tuple[float, int, int, int, Point]] = []
    for li, segs in enumerate(grid.tree_segments):
        own = grid.own_segments[li] if li < len(grid.own_segments) else ()
        cells = grid.cells[li]
        wx1, wy1 = grid.x0 + grid.w * pitch, grid.y0 + grid.h * pitch
        for a, b in segs:
            length = math.dist(a, b)
            if length < 2 * keep + JUNCTION_TOL:
                continue
            if (
                max(a[0], b[0]) < grid.x0
                or min(a[0], b[0]) > wx1
                or max(a[1], b[1]) < grid.y0
                or min(a[1], b[1]) > wy1
            ):
                continue  # outside the window: no node near it
            ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
            if abs(ang / 45.0 - round(ang / 45.0)) * 45.0 > _RIGHT_TOL_DEG:
                continue
            ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
            nx, ny = -uy, ux
            stops = [0.0, length]  # where the track is not a plain straight run
            for c, e in own:
                if (c, e) == (a, b):
                    continue
                for pt in (c, e):
                    if _seg_dist(pt, a, b) <= JUNCTION_TOL:
                        stops.append((pt[0] - a[0]) * ux + (pt[1] - a[1]) * uy)
                x = _cross_point(a, b, c, e)
                if x is not None:
                    stops.append((x[0] - a[0]) * ux + (x[1] - a[1]) * uy)
                for pt in (a, b):  # a track overlapping this one collinearly
                    inner = math.dist(pt, c) > JUNCTION_TOL and math.dist(pt, e) > JUNCTION_TOL
                    if inner and _seg_dist(pt, c, e) <= JUNCTION_TOL:
                        stops.append((pt[0] - a[0]) * ux + (pt[1] - a[1]) * uy)
            toward = {
                1.0: _dir_index((0.0, 0.0), (-nx, -ny)),
                -1.0: _dir_index((0.0, 0.0), (nx, ny)),
            }
            seen: set[int] = set()
            for k in range(math.ceil(length / pitch) + 1):
                t = min(length, k * pitch)
                ci, cj = grid.cell(a[0] + ux * t, a[1] + uy * t)
                for j in range(cj - 1, cj + 2):
                    for i in range(ci - 1, ci + 2):
                        if not grid.inside(i, j) or (n := j * w + i) in seen:
                            continue
                        seen.add(n)
                        p = grid.xy(i, j)
                        off = (p[0] - a[0]) * nx + (p[1] - a[1]) * ny
                        along = (p[0] - a[0]) * ux + (p[1] - a[1]) * uy
                        if abs(off) > reach or any(abs(along - st) < keep for st in stops):
                            continue
                        if cells[n] == BLOCKED:
                            continue
                        land = (round(a[0] + ux * along, 6), round(a[1] + uy * along, 6))
                        if grid.own_exempt(li, land) or _in_pad(land, req.b) or in_zone(land):
                            continue
                        if abs(off) <= 1e-9:
                            bits = (1 << toward[1.0]) | (1 << toward[-1.0])
                        else:
                            bits = 1 << toward[1.0 if off > 0 else -1.0]
                        d2 = (p[0] - req.a.x) ** 2 + (p[1] - req.a.y) ** 2
                        cands.append((d2, li, n, bits, land))
    cands.sort(key=lambda c: c[0])
    for _d2, li, n, bits, land in cands[:TREE_GOALS_MAX]:
        old = out[li].get(n)
        out[li][n] = (bits | (old[0] if old else 0), old[1] if old else land)
    return out


def reverse_path(path: RoutePath) -> RoutePath:
    """The same copper walked from the other end."""
    segs = tuple((layer, q, p, w) for layer, p, q, w in reversed(path.segments))
    return RoutePath(segments=segs, vias=tuple(reversed(path.vias)), length=path.length)


def _search(
    grid: ObstacleGrid,
    req: RouteRequest,
    start_pad: Pad,
    start_n: int,
    start_layers: list[int],
    goal_n: int,
    at_goal: Callable[[int, int, int], bool],
    target: tuple[int, int] | None,
    max_expansions: int,
    deadline: float | None = None,
    *,
    extra_goals: Sequence[int] = (),
    entry_ok: bool = False,
    goal_pieces: Sequence[tuple[Point, Point]] = (),
    end_of: Callable[[int, int, int], Point | None] | None = None,
) -> RoutePath | None:
    """The A* core. `at_goal(node, layer, dir)` marks goal states; with `build` a goal state
    is accepted only when its snapped path passes `valid_path` (→ the RoutePath), else any
    goal state → True. `target` = goal node (i, j) for the heuristic (None → Dijkstra).
    `extra_goals` (tree goal nodes, R3) seed the coarse field too, and the heuristic's
    distance is the smaller of the octile distance to `target` and the Euclidean distance
    to `goal_pieces` (board-coordinate segments covering the tree goals' landing points);
    `end_of(node, layer, dir)` gives a goal state's landing point (None = pad b).
    Budget spent or no goal → None (`_escapes`: budget spent → True)."""
    nl = len(grid.layers)
    w = grid.w
    pitch = grid.pitch
    start_dirs = sorted(pad_dirs(start_pad))
    cells = grid.cells
    via_blocked = grid.via_blocked
    cheap = grid.cheap
    escape = grid.escape
    allow_vias = req.allow_vias and nl > 1

    off = [di + dj * w for di, dj in DIRS]
    step = [pitch * (math.sqrt(2.0) if di and dj else 1.0) for di, dj in DIRS]
    lf = [1.0 if k == 0 else LAYER_FACTOR for k in range(nl)]
    c_min, j_min = CHAMFER_MIN(req.width), JOG_MIN(req.width)
    need_max = max(c_min, j_min)
    cap = [max(1, math.ceil(need_max / s - _EPS)) for s in step]
    kk = max(cap) + 1
    kmin = [max(1, math.ceil(min(c_min, j_min) / st - _EPS)) for st in step]
    # may[d][s][k][ns]: a turn of sense ns (1 = +45°, 2 = -45°) after k steps in direction d
    may = [
        [
            [
                [
                    True,
                    s == 0 or k * step[d] + _EPS >= (c_min if s == 1 else j_min),
                    s == 0 or k * step[d] + _EPS >= (c_min if s == 2 else j_min),
                ]
                for k in range(kk)
            ]
            for s in range(3)
        ]
        for d in range(8)
    ]
    hf = HEURISTIC_WEIGHT * (PAIR_BAND_FACTOR if cheap else 1.0)
    diag = math.sqrt(2.0) - 1.0

    use_coarse = USE_COARSE and target is not None
    gi, gj = target if target is not None else (0, 0)
    # the tree goals as straight pieces in lattice units (i, j): Euclidean distance to them
    # is a lower bound of the octile path length
    pieces = [
        ((p[0] - grid.x0) / pitch, (p[1] - grid.y0) / pitch, q[0] - p[0], q[1] - p[1])
        for p, q in goal_pieces
    ]
    pieces = [(i0, j0, di / pitch, dj / pitch) for i0, j0, di, dj in pieces]
    cf, cw, cdist = (
        coarse_distances(grid, gi, gj, (start_n, goal_n, *extra_goals), extra_goals)
        if use_coarse
        else (1, 1, [])
    )
    cstep = cf * pitch
    slack = 2.0 * cstep * math.sqrt(2.0)
    inf = math.inf

    def h(n: int) -> float:
        if target is None:
            return 0.0
        i, j = n % w, n // w
        dx = abs(i - gi)
        dy = abs(j - gj)
        octile = pitch * (max(dx, dy) + diag * min(dx, dy))
        for i0, j0, di, dj in pieces:
            ll = di * di + dj * dj
            t = 0.0 if ll <= 0 else max(0.0, min(1.0, ((i - i0) * di + (j - j0) * dj) / ll))
            e = pitch * math.hypot(i - i0 - t * di, j - j0 - t * dj)
            if e < octile:
                octile = e
        if not use_coarse:
            return hf * octile
        c = cdist[(j // cf) * cw + i // cf]
        if c == inf:
            return inf
        return hf * max(octile, c - slack)

    def key(n: int, li: int, d: int, s: int, k: int, z: int = 0) -> int:
        return ((((n * nl + li) * _ND + d) * _NZ + z) * 3 + s) * kk + k

    def decode(x: int) -> tuple[int, int]:
        x //= kk * 3 * _NZ * _ND
        return x // nl, x % nl  # node, layer

    sd = 3 * kk
    exit_cap = max(1, math.ceil(math.hypot(start_pad.w, start_pad.h) / pitch) + 2)
    ez = entry_nodes(grid, (req.a, req.b), req.width)  # bends there cost ENTRY_TURN_COST more
    zt = _ZONE_NEXT if not entry_ok else None  # R4: loops / jogs never formed in a zone
    try:  # per layer: node -> directions that make a T with own copper
        tj = junction_masks(grid, deadline)
    except DeadlineExpired:
        return None
    hc: dict[int, float] = {}  # heuristic per node (shared by its states)
    best: dict[int, float] = {}
    parent: dict[int, int] = {}
    closed: set[int] = set()
    heap: list[tuple[float, float, int, float, int, int, int, int, int, int]] = []
    counter = 0
    for li in start_layers:
        k0 = key(start_n, li, _START_D, 0, 0)
        best[k0] = 0.0
        parent[k0] = -1
        heap.append((h(start_n), 0.0, counter, 0.0, start_n, li, _START_D, 0, 0, 0))
        counter += 1
    heapq.heapify(heap)
    expansions = 0
    rejects = 0
    push = heapq.heappush
    pop = heapq.heappop
    blocked = BLOCKED

    while heap:
        _f, _ng, _c, g, n, li, d, s, k, z = pop(heap)
        here = ((((n) * nl + li) * _ND + d) * _NZ + z) * sd + (s) * kk + k
        if here in closed or g > best.get(here, math.inf):
            continue
        closed.add(here)  # weighted A* without re-opening: bounded by HEURISTIC_WEIGHT
        if at_goal(n, li, d):
            if deadline is not None and time.monotonic() > deadline:
                return None
            path = _build(here, parent, decode, grid, req, end_of(n, li, d) if end_of else None)
            ok = path is not None and valid_path(path, grid, req, entry_ok=entry_ok)
            if ok and path is not None and pad_exits_ok(path, req):
                return path
            rejects += 1
            if rejects > MAX_GOAL_REJECTS:
                return None
        expansions += 1
        if expansions > max_expansions:
            return None
        if deadline is not None and not expansions & 4095 and time.monotonic() > deadline:
            return None
        lay = cells[li]
        band = cheap[li] if cheap else None
        esc = escape[li] if escape else None
        factor = lf[li]
        tjl = tj[li]
        if d >= 8:
            # leaving the start pad: straight along an allowed direction until outside
            # the pad outline (pad_dirs holds for the whole way out); after a via: any
            moves = start_dirs if d == _START_D else range(8)
            for nd in moves:
                m = n
                c = 0.0
                for _t in range(exit_cap if d == _START_D else 1):
                    if tjl and (tjl.get(m, 0) >> nd) & 1:  # this step meets own copper at 90°
                        m = -1
                        break
                    m += off[nd]
                    if lay[m] == blocked and m != goal_n:
                        m = -1
                        break
                    c += (
                        step[nd]
                        * factor
                        * (PAIR_BAND_FACTOR if band is not None and band[m] else 1.0)
                        * (ESCAPE_COST_FACTOR if esc is not None and esc[m] else 1.0)
                    )
                    if (
                        d != _START_D
                        or m == goal_n
                        or not _in_pad(grid.xy(m % w, m // w), start_pad)
                    ):
                        break
                if m < 0:
                    continue
                ng = g + c
                nk = ((((m) * nl + li) * _ND + nd) * _NZ + 0) * sd + (0) * kk + 0
                if ng < best.get(nk, inf) and nk not in closed:
                    hm = hc.get(m)
                    if hm is None:
                        hm = hc[m] = h(m)
                    if hm == inf:
                        continue
                    best[nk] = ng
                    parent[nk] = here
                    push(heap, (ng + hm, -ng, counter, ng, m, li, nd, 0, 0, 0))
                    counter += 1
        else:
            # straight on
            m = n + off[d]
            if (lay[m] != blocked or m == goal_n) and not (tjl and (tjl.get(n, 0) >> d) & 1):
                c = step[d] * factor
                if band is not None and band[m]:
                    c *= PAIR_BAND_FACTOR
                if esc is not None and esc[m]:
                    c *= ESCAPE_COST_FACTOR
                s2, k2 = s, k + 1
                if s2 == 0 or k2 >= cap[d] or k2 * step[d] + _EPS >= need_max:
                    s2, k2 = 0, 0
                ng = g + c
                z2 = z if z and m in ez else 0
                base = (((m) * nl + li) * _ND + d) * _NZ
                nk = (base + z2) * sd + (s2) * kk + k2
                if (
                    ng < best.get(nk, inf)
                    and nk not in closed
                    and not (s2 and best.get((base + z2) * sd, inf) <= ng)
                    and not (z2 and best.get(base * sd + (s2) * kk + k2, inf) <= ng)
                ):
                    hm = hc.get(m)
                    if hm is None:
                        hm = hc[m] = h(m)
                    if hm != inf:
                        best[nk] = ng
                        parent[nk] = here
                        push(heap, (ng + hm, -ng, counter, ng, m, li, d, s2, k2, z2))
                        counter += 1
            # a 45° bend here, then straight on for the shortest legal piece in one move
            # (no bend can follow sooner; the goal may be reached on the way)
            in_zone = n in ez
            for nd, ns in (((d + 1) & 7, 1), ((d - 1) & 7, 2)):
                if not may[d][s][k][ns]:
                    continue
                zb = 0
                if zt is not None and in_zone:
                    zb = zt[z][ns]
                    if zb < 0:  # a jog (opposite sense) or a third bend (135°: a loop)
                        continue
                o, st = off[nd], step[nd]
                # a second bend soon after another one in an entry zone: a jog or a loop
                c = TURN_COST + (ENTRY_TURN_COST if s and n in ez else 0.0)
                m = n
                for t in range(1, kmin[nd] + 1):
                    if tjl and (tjl.get(m, 0) >> nd) & 1:  # this step meets own copper at 90°
                        break
                    m += o
                    if lay[m] == blocked and m != goal_n:
                        break
                    c += (
                        st
                        * factor
                        * (PAIR_BAND_FACTOR if band is not None and band[m] else 1.0)
                        * (ESCAPE_COST_FACTOR if esc is not None and esc[m] else 1.0)
                    )
                    if t < kmin[nd] and m != goal_n:
                        continue
                    s2, k2 = ns, t
                    if k2 >= cap[nd] or k2 * st + _EPS >= need_max:
                        s2, k2 = 0, 0
                    ng = g + c
                    z2 = zb if zb and m in ez else 0
                    nk = ((((m) * nl + li) * _ND + nd) * _NZ + z2) * sd + (s2) * kk + k2
                    if ng < best.get(nk, inf) and nk not in closed:
                        hm = hc.get(m)
                        if hm is None:
                            hm = hc[m] = h(m)
                        if hm != inf:
                            best[nk] = ng
                            parent[nk] = here
                            push(heap, (ng + hm, -ng, counter, ng, m, li, nd, s2, k2, z2))
                            counter += 1
            if allow_vias and not via_blocked[n]:
                ng = g + VIA_COST
                for l2 in range(nl):
                    if l2 == li or cells[l2][n] == blocked:
                        continue
                    nk = ((((n) * nl + l2) * _ND + _VIA_D) * _NZ + 0) * sd + (0) * kk + 0
                    if ng < best.get(nk, math.inf):
                        best[nk] = ng
                        parent[nk] = here
                        push(heap, (ng + h(n), -ng, counter, ng, n, l2, _VIA_D, 0, 0, 0))
                        counter += 1
    return None


def _build(
    here: int,
    parent: dict[int, int],
    decode: Callable[[int], tuple[int, int]],
    grid: ObstacleGrid,
    req: RouteRequest,
    end: Point | None = None,
) -> RoutePath | None:
    states: list[tuple[int, int, int]] = []
    x = here
    while x != -1:
        n, li = decode(x)
        states.append((li, n % grid.w, n // grid.w))
        x = parent[x]
    states.reverse()
    try:
        return to_path(states, grid, req, end=end)
    except SnapError:
        return None


FIRST_TRY_EXPANSIONS = 500_000  # budget of solve's first (default-window) search
RETRY_STRICT_EXPANSIONS = 1_000_000  # budget of solve's doubled-window retry (R3)
# wall-clock cap of one solve / solve_pair (both searches together), below run_cpu's 30 s
SOLVE_TIME_LIMIT_S = 25.0


def solve(
    model: BoardModel,
    req: RouteRequest,
    *,
    margin: float = 0.0,
    max_expansions: int = 2_000_000,
    entry_ok: bool = False,
) -> RoutePath | None:
    """`run_cpu` entry point (B-C12): route on the default window, then exactly once on the
    doubled window. The retry runs from pad `b` (its lattice aligned to `b`, the result
    reversed): a tight squeeze that one lattice misses, the other often fits. The first
    search gets at most FIRST_TRY_EXPANSIONS, the retry at most RETRY_STRICT_EXPANSIONS;
    both together stop after SOLVE_TIME_LIMIT_S (→ None). `RouteTooLarge` from either window
    propagates. A pad-entry loop or jog (M16 b / c) is never formed (R4, owner 2026-11-27:
    both hard, no fallback that allows them); `entry_ok` (tests, explicit callers only)
    lifts that rule in both windows."""
    deadline = time.monotonic() + SOLVE_TIME_LIMIT_S
    first = min(max_expansions, FIRST_TRY_EXPANSIONS)
    try:
        grid = build_grid(model, req, margin=margin, deadline=deadline)
        path = route(grid, req, max_expansions=first, deadline=deadline, entry_ok=entry_ok)
        if path is not None:
            return path
        check_deadline(deadline)
        rev = replace(req, a=req.b, b=req.a)
        grid2 = build_grid(model, rev, margin=margin, window_mm=2 * WINDOW_MM, deadline=deadline)
        retry = min(max_expansions, RETRY_STRICT_EXPANSIONS)
        back = route(grid2, rev, max_expansions=retry, deadline=deadline, entry_ok=entry_ok)
    except DeadlineExpired:
        return None
    return reverse_path(back) if back is not None else None
