"""Coupled pairs, the pair entry point and coupling numbers (B-C12, P5a Task 4).

Coupled (spec call 24, straight-ish pairs): when both pad pairs lie close together, one
centreline of width `2w + gap` is routed between the pad-pair midpoints on the first layer
(no vias) with both nets treated as one; it is offset by ±(gap + w)/2 — mitred at the 45°
bends, so both offsets stay octilinear — and each offset's ends are snapped to its pads.
Both results are checked like any route (B-C5 bends, exact clearance incl. the partner).
Any failure → fallback: `p` alone, then `n` with the partner's copper as an obstacle
(centre distance ≥ max(gap, clearance) + w) and a cheaper band at the coupling distance.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import replace

from copperchat.board.geom import JOG_MIN, _pad_on_layer
from copperchat.board.model import BoardModel, Pad, Point
from copperchat.board.router.astar import valid_path
from copperchat.board.router.astar import FIRST_TRY_EXPANSIONS, reverse_path
from copperchat.board.router.astar import route as _route
from copperchat.board.router.grid import (
    WINDOW_MM,
    ObstacleGrid,
    RoutePath,
    RouteRequest,
    _pad_shape,
    band_cells,
    build_grid,
)
from copperchat.board.router.simplify import SnapError, layer_runs, merge_collinear, snap_ends

COUPLE_SAMPLE_MM = 0.1
_PAD_PAIR = "~pair"


def _pair_clearance(p: RouteRequest, n: RouteRequest, gap: float) -> float:
    return max(gap, p.clearance, n.clearance)


def coupled_eligible(p: RouteRequest, n: RouteRequest, gap: float) -> bool:
    """Both pad pairs within `2 × (w + gap) + max pad size` of each other."""
    w = max(p.width, n.width)
    size = max(max(q.w, q.h) for q in (p.a, p.b, n.a, n.b))
    lim = 2 * (w + gap) + size
    return math.dist((p.a.x, p.a.y), (n.a.x, n.a.y)) <= lim and math.dist((p.b.x, p.b.y), (n.b.x, n.b.y)) <= lim


def _merge_nets(model: BoardModel, old: str, new: str) -> BoardModel:
    """`model` with net `old` renamed to `new` (the centreline treats both as own copper)."""
    parts = tuple(
        replace(pt, pads=tuple(replace(q, net=new) if q.net == old else q for q in pt.pads)) for pt in model.parts
    )
    return replace(
        model,
        parts=parts,
        tracks=tuple(replace(t, net=new) if t.net == old else t for t in model.tracks),
        arcs=tuple(replace(a, net=new) if a.net == old else a for a in model.arcs),
        vias=tuple(replace(v, net=new) if v.net == old else v for v in model.vias),
    )


def _point_pad(net: str, at: Point, size: float, number: str) -> Pad:
    return Pad(
        ref=_PAD_PAIR,
        number=number,
        net=net,
        x=at[0],
        y=at[1],
        w=size,
        h=size,
        rot=0.0,
        shape="rect",
        layers=frozenset({"*.Cu"}),
        smd=False,
        drill=None,
    )


def offset_polyline(points: Sequence[Point], o: float) -> tuple[Point, ...]:
    """`points` shifted by `o` to the left of the travel direction (on screen with y down:
    left = (u.y, −u.x)), mitred at every vertex (a 45° bend keeps both offsets octilinear)."""
    pts = list(merge_collinear(points))
    normals = []
    for p, q in itertools.pairwise(pts):
        ux, uy = q[0] - p[0], q[1] - p[1]
        n = math.hypot(ux, uy)
        normals.append((uy / n, -ux / n))
    out = [(pts[0][0] + o * normals[0][0], pts[0][1] + o * normals[0][1])]
    for k in range(1, len(pts) - 1):
        n1, n2 = normals[k - 1], normals[k]
        dot = n1[0] * n2[0] + n1[1] * n2[1]
        f = o / (1.0 + dot)
        out.append((pts[k][0] + f * (n1[0] + n2[0]), pts[k][1] + f * (n1[1] + n2[1])))
    out.append((pts[-1][0] + o * normals[-1][0], pts[-1][1] + o * normals[-1][1]))
    return tuple(out)


def _as_path(layer: str, pts: Sequence[Point]) -> RoutePath:
    segs = tuple((layer, p, q) for p, q in itertools.pairwise(pts))
    return RoutePath(segments=segs, vias=(), length=sum(math.dist(p, q) for _l, p, q in segs))


def _partner(path: RoutePath, need: float) -> list[tuple[str, tuple]]:
    return [(layer, ("s", *p, *q, need)) for layer, p, q in path.segments]


def _check(
    model: BoardModel, req: RouteRequest, path: RoutePath, other: RouteRequest, partner: RoutePath, pc: float, margin: float
) -> bool:
    """`path` passes the B-C5 bends and exact clearance; `margin` applies to all other-net
    copper except the partner's (its pads and path keep the pair clearance `pc`)."""
    need = pc + req.width / 2
    extra = _partner(partner, need + other.width / 2)
    pads = [q for pt in model.parts for q in pt.pads if q.net == other.net]
    extra += [(layer, _pad_shape(q, need)) for q in pads for layer in req.layers if _pad_on_layer(q, layer)]
    grid = build_grid(_merge_nets(model, other.net, req.net), req, margin=margin, extra=extra)
    return valid_path(path, grid, req)


def _lead(p_pad: Pad, n_pad: Pad, towards: Point) -> Point | None:
    """Unit direction leaving a pad pair at right angles to it (the side facing `towards`);
    None when the pad pair is not on an octilinear axis (±1°)."""
    vx, vy = n_pad.x - p_pad.x, n_pad.y - p_pad.y
    length = math.hypot(vx, vy)
    if length < 1e-9:
        return None
    ang = math.degrees(math.atan2(vy, vx)) % 45.0
    if min(ang, 45.0 - ang) > 1.0:
        return None
    ux, uy = -vy / length, vx / length
    mx, my = (p_pad.x + n_pad.x) / 2, (p_pad.y + n_pad.y) / 2
    if (towards[0] - mx) * ux + (towards[1] - my) * uy < 0:
        ux, uy = -ux, -uy
    return (ux, uy)


def _coupled(
    model: BoardModel, p: RouteRequest, n: RouteRequest, gap: float, margin: float
) -> tuple[RoutePath, RoutePath] | None:
    w = max(p.width, n.width)
    clear = max(p.clearance, n.clearance)
    pc = _pair_clearance(p, n, gap)
    cw = 2 * w + pc
    ma = ((p.a.x + n.a.x) / 2, (p.a.y + n.a.y) / 2)
    mb = ((p.b.x + n.b.x) / 2, (p.b.y + n.b.y) / 2)
    ua, ub = _lead(p.a, n.a, mb), _lead(p.b, n.b, ma)
    if ua is None or ub is None:
        return None
    # straight leads at right angles out of each pad pair, so the offsets meet the pads
    lead = JOG_MIN(cw)
    ma2 = (ma[0] + lead * ua[0], ma[1] + lead * ua[1])
    mb2 = (mb[0] + lead * ub[0], mb[1] + lead * ub[1])
    merged = _merge_nets(model, n.net, p.net)
    layer = p.layers[0]
    centre = RouteRequest(
        net=p.net,
        a=_point_pad(p.net, ma2, 0.1, "a"),
        b=_point_pad(p.net, mb2, 0.1, "b"),
        width=cw,
        clearance=clear,
        layers=(layer,),
        allow_vias=False,
        via=p.via,
    )
    path = _route(build_grid(merged, centre, margin=margin), centre)
    if path is None or len(layer_runs(path)) != 1:
        return None
    pts = merge_collinear([ma, *layer_runs(path)[0][1], mb])
    if len(pts) < 2:
        return None
    o = (pc + w) / 2
    u = (pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
    left = (u[1], -u[0])
    side = 1.0 if (p.a.x - ma[0]) * left[0] + (p.a.y - ma[1]) * left[1] >= 0 else -1.0
    try:
        pp = snap_ends(offset_polyline(pts, side * o), (p.a.x, p.a.y), (p.b.x, p.b.y), p.width)
        nn = snap_ends(offset_polyline(pts, -side * o), (n.a.x, n.a.y), (n.b.x, n.b.y), n.width)
    except SnapError:
        return None
    rp, rn = _as_path(layer, pp), _as_path(layer, nn)
    if not _check(model, p, rp, n, rn, pc, margin) or not _check(model, n, rn, p, rp, pc, margin):
        return None
    return rp, rn


def _solo(
    model: BoardModel, req: RouteRequest, margin: float, retry: bool, extra=(), band=None
) -> RoutePath | None:  # type: ignore[no-untyped-def]
    # like `solve`: the retry runs from pad b on the doubled window
    tries = [(req, None, FIRST_TRY_EXPANSIONS)]
    if retry:
        tries.append((replace(req, a=req.b, b=req.a), 2 * WINDOW_MM, 2_000_000))
    for k, (r, window, budget) in enumerate(tries):
        grid = build_grid(model, r, margin=margin, window_mm=window, extra=extra)
        if band is not None:
            grid.cheap = band(grid)
        path = _route(grid, r, max_expansions=budget)
        if path is not None:
            return path if k == 0 else reverse_path(path)
    return None


def _fallback(
    model: BoardModel, p: RouteRequest, n: RouteRequest, gap: float, margin: float, retry: bool
) -> tuple[RoutePath, RoutePath] | None:
    pp = _solo(model, p, margin, retry)
    if pp is None:
        return None
    pc = _pair_clearance(p, n, gap)
    need = pc + (p.width + n.width) / 2  # the pair keeps its own spacing (no margin)
    target = gap + (p.width + n.width) / 2

    def band(grid: ObstacleGrid) -> tuple[bytearray, ...]:
        return band_cells(grid, pp.segments, target - grid.pitch, target + grid.pitch)

    pn = _solo(model, n, margin, retry, extra=_partner(pp, need), band=band)
    if pn is None:
        return None
    return pp, pn


def route_pair(
    model: BoardModel, p: RouteRequest, n: RouteRequest, gap: float
) -> tuple[RoutePath, RoutePath] | None:
    """Coupled pair when the pads allow it, else the fallback (B-C12); default windows only."""
    if coupled_eligible(p, n, gap):
        got = _coupled(model, p, n, gap, 0.0)
        if got is not None:
            return got
    return _fallback(model, p, n, gap, 0.0, retry=False)


def solve_pair(
    model: BoardModel, p: RouteRequest, n: RouteRequest, gap: float, *, margin: float = 0.0
) -> tuple[RoutePath, RoutePath] | None:
    """`run_cpu` entry point (B-C12): the coupled attempt, then the fallback where each net
    gets the default window and exactly one doubled-window retry (like `solve`)."""
    if coupled_eligible(p, n, gap):
        got = _coupled(model, p, n, gap, margin)
        if got is not None:
            return got
    return _fallback(model, p, n, gap, margin, retry=True)


def _pt_seg(p: Point, a: Point, b: Point) -> tuple[float, float]:
    """(distance, projection parameter clamped to [0, 1])."""
    vx, vy = b[0] - a[0], b[1] - a[1]
    ll = vx * vx + vy * vy
    t = 0.0 if ll == 0 else ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / ll
    tc = max(0.0, min(1.0, t))
    return math.hypot(p[0] - a[0] - tc * vx, p[1] - a[1] - tc * vy), t


def pair_coupling(a: RoutePath, b: RoutePath) -> dict[str, float]:
    """{length_mismatch, gap_min, gap_max}: centre-to-centre distance from points every
    0.1 mm along `a` to the nearest segment of `b`, counted where that segment is parallel to
    `a`'s segment on the same layer and the point projects onto it (the coupled stretch);
    no coupled stretch → both `inf`."""
    gaps: list[float] = []
    for layer, p, q in a.segments:
        length = math.dist(p, q)
        if length <= 0:
            continue
        ux, uy = (q[0] - p[0]) / length, (q[1] - p[1]) / length
        others = [(s, e) for lay, s, e in b.segments if lay == layer and math.dist(s, e) > 0]
        if not others:
            continue
        steps = max(1, math.ceil(length / COUPLE_SAMPLE_MM))
        for k in range(steps + 1):
            pt = (p[0] + ux * length * k / steps, p[1] + uy * length * k / steps)
            best = min(others, key=lambda se: _pt_seg(pt, *se)[0])
            d, t = _pt_seg(pt, *best)
            s, e = best
            lb = math.dist(s, e)
            vx, vy = (e[0] - s[0]) / lb, (e[1] - s[1]) / lb
            if abs(ux * vy - uy * vx) <= 1e-6 and -1e-9 <= t <= 1 + 1e-9:
                gaps.append(d)
    return {
        "length_mismatch": abs(a.length - b.length),
        "gap_min": min(gaps) if gaps else math.inf,
        "gap_max": max(gaps) if gaps else math.inf,
    }
