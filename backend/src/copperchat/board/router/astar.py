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
"""

from __future__ import annotations

import heapq
import itertools
import math
import re
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import replace

from copperchat.board.geom import CHAMFER_MIN, JOG_MIN, _in_pad, _pad_on_layer, analyse_bends
from copperchat.board.model import BoardModel, Pad, Point
from copperchat.board.router.grid import (
    BLOCKED,
    WINDOW_MM,
    DeadlineExpired,
    ObstacleGrid,
    RoutePath,
    RouteRequest,
    build_grid,
    check_deadline,
    path_clear,
)
from copperchat.board.router.simplify import SnapError, layer_runs, merge_collinear, to_path

TURN_COST = 1.0  # mm-equivalent per 45° bend (0.3 planned; 1.0 measured: fewer bends, same speed)
VIA_COST = 2.0  # mm-equivalent per via
LAYER_FACTOR = 1.2  # step cost on every layer but the request's first
PAIR_BAND_FACTOR = 0.8  # step cost inside a grid's `cheap` band (pair fallback)
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
_EPS = 1e-9


def pad_dirs(pad: Pad) -> frozenset[int]:
    """Directions a track may leave (or enter) `pad` in: the long axis both ways and ±45°
    to it — 6 of 8; square and round pads: all 8. The axis is the pad's rotation rounded to
    the nearest 45°."""
    if pad.shape == "circle" or abs(pad.w - pad.h) <= 1e-9:
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
    last = _crossing_dir(runs[-1][1][::-1], req.b)
    return (first is None or first in pad_dirs(req.a)) and (last is None or last in pad_dirs(req.b))


def valid_path(path: RoutePath, grid: ObstacleGrid, req: RouteRequest) -> bool:
    """The path starts on a layer of pad `a` and ends on one of pad `b`; `analyse_bends` has
    no failure on any layer run (the end-bend exemption only for a bend inside its pad —
    stricter than the board metric, which exempts every pad-end bend); every segment and
    via keeps its exact clearance, also between the path's own vias."""
    runs = layer_runs(path)
    if not runs:
        return False
    if not (_pad_on_layer(req.a, runs[0][0]) and _pad_on_layer(req.b, runs[-1][0])):
        return False
    last = len(runs) - 1
    for k, (_layer, raw) in enumerate(runs):
        pts = merge_collinear(raw)
        # the pad-entry exemption only for an end bend that lies inside its pad
        ends = (
            k == 0 and len(pts) >= 3 and _in_pad(pts[1], req.a),
            k == last and len(pts) >= 3 and _in_pad(pts[-2], req.b),
        )
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
    grid: ObstacleGrid, gi: int, gj: int, forced: Iterable[int]
) -> tuple[int, int, list[float]]:
    """(factor, coarse width, distance to the goal per coarse cell). Coarse cells are
    COARSE_MM squares; two neighbouring cells are linked when a fine move (on any layer)
    joins a free node of one to a free node of the other, and a diagonal neighbour also
    through either side cell. Every real path maps to a linked cell sequence, so
    `distance - slack` (two cell diagonals) stays a near lower bound; cells the goal cannot
    be reached from are `inf`."""
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
    goal = (gj // cf) * cw + gi // cf
    dist[goal] = 0.0
    heap = [(0.0, goal)]
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
) -> RoutePath | None:
    """The cheapest octilinear path from `req.a` to `req.b` on `grid` that passes the final
    checks, or None (no path in the window, `max_expansions` exhausted, or too many goal
    states rejected). One search (after two cheap feasibility checks); never rebuilds or
    widens the grid."""
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
                if direct.segments and valid_path(direct, grid, req):
                    return direct
        if goal_layers & set(a_layers):
            return None
        # stacked pads on different layers: search (a via may join them)

    def at_goal(n: int, li: int, d: int) -> bool:
        return n == goal_n and li in goal_layers and d in goal_dirs

    found = _search(
        grid, req, req.a, start_n, a_layers, goal_n, at_goal, (gi, gj), max_expansions, deadline
    )
    return found


def reverse_path(path: RoutePath) -> RoutePath:
    """The same copper walked from the other end."""
    segs = tuple((layer, q, p) for layer, p, q in reversed(path.segments))
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
) -> RoutePath | None:
    """The A* core. `at_goal(node, layer, dir)` marks goal states; with `build` a goal state
    is accepted only when its snapped path passes `valid_path` (→ the RoutePath), else any
    goal state → True. `target` = goal node (i, j) for the heuristic (None → Dijkstra).
    Budget spent or no goal → None (`_escapes`: budget spent → True)."""
    nl = len(grid.layers)
    w = grid.w
    pitch = grid.pitch
    start_dirs = sorted(pad_dirs(start_pad))
    cells = grid.cells
    via_blocked = grid.via_blocked
    cheap = grid.cheap
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
    cf, cw, cdist = coarse_distances(grid, gi, gj, (start_n, goal_n)) if use_coarse else (1, 1, [])
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
        if not use_coarse:
            return hf * octile
        c = cdist[(j // cf) * cw + i // cf]
        if c == inf:
            return inf
        return hf * max(octile, c - slack)

    def key(n: int, li: int, d: int, s: int, k: int) -> int:
        return (((n * nl + li) * _ND + d) * 3 + s) * kk + k

    def decode(x: int) -> tuple[int, int]:
        x //= kk * 3 * _ND
        return x // nl, x % nl  # node, layer

    sd = 3 * kk
    exit_cap = max(1, math.ceil(math.hypot(start_pad.w, start_pad.h) / pitch) + 2)
    hc: dict[int, float] = {}  # heuristic per node (shared by its states)
    best: dict[int, float] = {}
    parent: dict[int, int] = {}
    closed: set[int] = set()
    heap: list[tuple[float, float, int, float, int, int, int, int, int]] = []
    counter = 0
    for li in start_layers:
        k0 = key(start_n, li, _START_D, 0, 0)
        best[k0] = 0.0
        parent[k0] = -1
        heap.append((h(start_n), 0.0, counter, 0.0, start_n, li, _START_D, 0, 0))
        counter += 1
    heapq.heapify(heap)
    expansions = 0
    rejects = 0
    push = heapq.heappush
    pop = heapq.heappop
    blocked = BLOCKED

    while heap:
        _f, _ng, _c, g, n, li, d, s, k = pop(heap)
        here = (((n) * nl + li) * _ND + d) * sd + (s) * kk + k
        if here in closed or g > best.get(here, math.inf):
            continue
        closed.add(here)  # weighted A* without re-opening: bounded by HEURISTIC_WEIGHT
        if at_goal(n, li, d):
            if deadline is not None and time.monotonic() > deadline:
                return None
            path = _build(here, parent, decode, grid, req)
            if path is not None and valid_path(path, grid, req) and pad_exits_ok(path, req):
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
        factor = lf[li]
        if d >= 8:
            # leaving the start pad: straight along an allowed direction until outside
            # the pad outline (pad_dirs holds for the whole way out); after a via: any
            moves = start_dirs if d == _START_D else range(8)
            for nd in moves:
                m = n
                c = 0.0
                for _t in range(exit_cap if d == _START_D else 1):
                    m += off[nd]
                    if lay[m] == blocked and m != goal_n:
                        m = -1
                        break
                    c += (
                        step[nd]
                        * factor
                        * (PAIR_BAND_FACTOR if band is not None and band[m] else 1.0)
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
                nk = (((m) * nl + li) * _ND + nd) * sd + (0) * kk + 0
                if ng < best.get(nk, inf) and nk not in closed:
                    hm = hc.get(m)
                    if hm is None:
                        hm = hc[m] = h(m)
                    if hm == inf:
                        continue
                    best[nk] = ng
                    parent[nk] = here
                    push(heap, (ng + hm, -ng, counter, ng, m, li, nd, 0, 0))
                    counter += 1
        else:
            # straight on
            m = n + off[d]
            if lay[m] != blocked or m == goal_n:
                c = step[d] * factor
                if band is not None and band[m]:
                    c *= PAIR_BAND_FACTOR
                s2, k2 = s, k + 1
                if s2 == 0 or k2 >= cap[d] or k2 * step[d] + _EPS >= need_max:
                    s2, k2 = 0, 0
                ng = g + c
                nk = (((m) * nl + li) * _ND + d) * sd + (s2) * kk + k2
                if (
                    ng < best.get(nk, inf)
                    and nk not in closed
                    and not (
                        s2 and best.get((((m) * nl + li) * _ND + d) * sd + (0) * kk + 0, inf) <= ng
                    )
                ):
                    hm = hc.get(m)
                    if hm is None:
                        hm = hc[m] = h(m)
                    if hm != inf:
                        best[nk] = ng
                        parent[nk] = here
                        push(heap, (ng + hm, -ng, counter, ng, m, li, d, s2, k2))
                        counter += 1
            # a 45° bend here, then straight on for the shortest legal piece in one move
            # (no bend can follow sooner; the goal may be reached on the way)
            for nd, ns in (((d + 1) & 7, 1), ((d - 1) & 7, 2)):
                if not may[d][s][k][ns]:
                    continue
                o, st = off[nd], step[nd]
                c = TURN_COST
                m = n
                for t in range(1, kmin[nd] + 1):
                    m += o
                    if lay[m] == blocked and m != goal_n:
                        break
                    c += st * factor * (PAIR_BAND_FACTOR if band is not None and band[m] else 1.0)
                    if t < kmin[nd] and m != goal_n:
                        continue
                    s2, k2 = ns, t
                    if k2 >= cap[nd] or k2 * st + _EPS >= need_max:
                        s2, k2 = 0, 0
                    ng = g + c
                    nk = (((m) * nl + li) * _ND + nd) * sd + (s2) * kk + k2
                    if ng < best.get(nk, inf) and nk not in closed:
                        hm = hc.get(m)
                        if hm is None:
                            hm = hc[m] = h(m)
                        if hm != inf:
                            best[nk] = ng
                            parent[nk] = here
                            push(heap, (ng + hm, -ng, counter, ng, m, li, nd, s2, k2))
                            counter += 1
            if allow_vias and not via_blocked[n]:
                ng = g + VIA_COST
                for l2 in range(nl):
                    if l2 == li or cells[l2][n] == blocked:
                        continue
                    nk = (((n) * nl + l2) * _ND + _VIA_D) * sd + (0) * kk + 0
                    if ng < best.get(nk, math.inf):
                        best[nk] = ng
                        parent[nk] = here
                        push(heap, (ng + h(n), -ng, counter, ng, n, l2, _VIA_D, 0, 0))
                        counter += 1
    return None


def _build(
    here: int,
    parent: dict[int, int],
    decode: Callable[[int], tuple[int, int]],
    grid: ObstacleGrid,
    req: RouteRequest,
) -> RoutePath | None:
    states: list[tuple[int, int, int]] = []
    x = here
    while x != -1:
        n, li = decode(x)
        states.append((li, n % grid.w, n // grid.w))
        x = parent[x]
    states.reverse()
    try:
        return to_path(states, grid, req)
    except SnapError:
        return None


FIRST_TRY_EXPANSIONS = 500_000  # budget of solve's first (default-window) search
# wall-clock cap of one solve / solve_pair (both searches together), below run_cpu's 30 s
SOLVE_TIME_LIMIT_S = 25.0


def solve(
    model: BoardModel, req: RouteRequest, *, margin: float = 0.0, max_expansions: int = 2_000_000
) -> RoutePath | None:
    """`run_cpu` entry point (B-C12): route on the default window, then exactly once on the
    doubled window. The retry runs from pad `b` (its lattice aligned to `b`, the result
    reversed): a tight squeeze that one lattice misses, the other often fits. The first
    search gets at most FIRST_TRY_EXPANSIONS; both together stop after SOLVE_TIME_LIMIT_S
    (→ None). `RouteTooLarge` from either window propagates."""
    deadline = time.monotonic() + SOLVE_TIME_LIMIT_S
    try:
        grid = build_grid(model, req, margin=margin, deadline=deadline)
        path = route(
            grid, req, max_expansions=min(max_expansions, FIRST_TRY_EXPANSIONS), deadline=deadline
        )
        if path is not None:
            return path
        check_deadline(deadline)
        rev = replace(req, a=req.b, b=req.a)
        grid = build_grid(model, rev, margin=margin, window_mm=2 * WINDOW_MM, deadline=deadline)
        back = route(grid, rev, max_expansions=max_expansions, deadline=deadline)
    except DeadlineExpired:
        return None
    return reverse_path(back) if back is not None else None
