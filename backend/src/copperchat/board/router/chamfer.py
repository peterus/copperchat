"""Freerouting post-processor (B-C12, P6a): merge, chamfer, rip list. Pure, deterministic,
no kipy import; module-level `postprocess` runs in `run_cpu`.

Only the tracks whose KIIDs are given ("touched") change; every other copper item is an
obstacle only. Steps:

1. Drop zero-length touched tracks (< 0.001 mm) and exact duplicates (same net, layer,
   width, endpoints in either order; an untouched twin wins, else the smallest KIID). A
   touched piece shorter than the join tolerance (0.002 mm, below which `geom.polylines`
   snaps ends into one node) is dropped too and the tracks around it - or around a gap that
   short - get one shared nm-grid end, each moved along its own line (`_close_gaps`);
   untouched track, arc and via ends never move (such a piece stays and step 3 judges it).
   Then merge collinear same-net/layer/width touched tracks that share an endpoint with no third
   item at it (another track end or interior on that net/layer, a via, a pad on the layer).
2. Every 90-degree vertex of a polyline (`geom.polylines`) whose two corner tracks are
   touched is chamfered by one diagonal of length >= c from `(3w, 2.5w, 2w, 1.5w,
   CHAMFER_MIN(w))` (only c >= CHAMFER_MIN(w); first feasible wins). KiCad stores integer
   nanometres, so the new endpoints are computed on that grid: the corner vertex rounded
   to 1 nm, each leg's offset a = c / sqrt 2 taken along the leg's octilinear direction with
   every axis step rounded UP to whole nm (equal on both axes of a diagonal leg). The
   diagonal of two axis legs then has |dx| == |dy| (exactly 45 degrees), the chamfer of two
   diagonal legs is exactly axis-aligned, and its length is >= c - nothing changes when
   KiCad rounds. Every check below runs on these snapped points. c is feasible when the
   offset stays >= 0.001 mm inside both corner tracks and short of the neighbouring
   polyline vertices, no other same-net copper end (another track's or an arc's - arcs are
   invisible to `polylines`) lies on the cut-away corner (so a vertex where an arc ends is
   never moved; a 90-degree corner there is ripped `ninety_unfixable`), the diagonal
   inflated by w / 2 + clearance (per obstacle, as KiCad: the max of the net's class
   clearance, the obstacle net's class clearance and `rules["min_clearance"]`) overlaps no
   pad, track, arc or via of another net (diagonals placed earlier included), and
   `analyse_bends` of the changed stretch reports no failure. Adjacent corners share a
   leg: their offsets are chosen jointly (depth-first over each run of adjacent corners,
   larger c preferred from the run's start), so the straight piece left between them
   satisfies the chamfer/jog rules. The whole new
   polyline is re-checked by `analyse_bends`; a polyline with any unfixable corner (or any
   other failure, e.g. a corner with only one leg touched) is left unchanged and ripped in
   step 3 - never partly chamfered.
3. `analyse_tracks` on the result, per touched net and layer, against the same analysis of
   that net/layer's untouched tracks alone: every failing point (non-octilinear, 90-degree,
   acute, off-angle, off-pad T-junction, micro-jog, too-short chamfer) not also found
   (within 0.003 mm, same kind) without the touched copper is ours - even when the point
   itself lies on untouched copper (e.g. a micro-jog whose short piece is an untouched
   stub between two touched diagonals) - and rips connections with the reason
   `non_octilinear`, `ninety_unfixable` (90-degree corners; T-junctions are legal since
   round 2), `acute`,
   `off_angle`, `micro_jog`, `short_chamfer`. Failures of the untouched copper alone are
   not ours and are ignored. Safety net (M4 / M7 as KiCad stores the copper): on the
   nm-rounded result, every `geom.polylines` polyline containing touched copper is
   `analyse_bends`-checked as a whole (pad ends as M4 / M7) against the untouched-only
   polylines the same way, and a cluster of distinct ends within the join tolerance that
   holds a touched end (whose walked geometry depends on track order) rips
   `non_octilinear` unless it is a collinear gap or one node turning no track by more
   than a quarter of the angle tolerance.
4. Per connection of a touched net: routed (pad to pad through copper) before but not
   after -> `disconnected` (safety net; the checks above should make it unreachable). Per
   connection of a touched non-ground net (octilinear distance >= `M5_MIN_MM`, as M5):
   ratio > 2.0 and extra > 5 mm -> `detour`; routed length > 50 % of the outline's bbox
   diagonal and ratio > 1.5 -> `long_detour` (M6 alone never rips).

Rip attribution: a defect rips every connection (`metrics.connections`, pads ordered by
natural (ref, number)) whose shortest routed path (the M5 copper graph: track ends snapped,
T-splits, vias and pads at 0 mm) uses a track (touched or not) within 0.003 mm of the
defect point. When none does (e.g. a dangling stub), every connection of that net whose
path uses that layer is ripped; when none does either, every connection of the net. A net
with fewer than two pads has no connection and therefore no rip key.

`replace` semantics: merging N tracks emits `(first kiid, (merged,))` (first = the chain
track at its lexicographically smaller end) and `(other kiid, ())`; a chamfer emits
`(kiid_in, (shortened_in,))` and `(kiid_out, (shortened_out, diagonal))` in polyline
order (a track between two corners: its kiid -> (shortened at both ends, the diagonal of
the corner it starts at)); zero-length, duplicate and sub-join-tolerance tracks
`(kiid, ())`; a track whose end step 1 moved `(kiid, (moved,))`. Every new or changed
`Track` has `kiid=""`, the corner's net, layer and width and `locked=False`; every endpoint
a chamfer or step 1 creates lies on the 1 nm grid (the far ends of shortened tracks keep
their input coordinates). Only changed KIIDs appear.
"""

from __future__ import annotations

import heapq
import itertools
import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import partial

from copperchat.board.classify import _ground_nets
from copperchat.board.geom import (
    _JOIN_TOL,
    ANGLE_TOL_DEG,
    CHAMFER_MIN,
    BendReport,
    TrackIndex,
    _centres,
    _graph,
    _Grid,
    _in_pad,
    _interior_t,
    _Near,
    _Nodes,
    _pad_on_layer,
    _walk_polylines,
    analyse_bends,
    analyse_tracks,
    convex_hull,
    octi_dist,
    pad_entry_ends,
    polygons_overlap,
    polylines,
)
from copperchat.board.metrics import (
    HARD,
    M5_MIN_MM,
    REVIEW,
    _arc_points,
    _mst,
    _pad_key,
    _pad_poly,
    outline_polygon,
)
from copperchat.board.model import BoardModel, Pad, Part, Point, Track, Via
from copperchat.kicad_tools.filtering import natural_key

__all__ = ["CHAMFER_FACTORS", "ZERO_LEN", "PostResult", "postprocess"]

ZERO_LEN = 0.001  # mm: shorter tracks are dropped; every remaining piece is at least this
CHAMFER_FACTORS = (3.0, 2.5, 2.0, 1.5)  # x width, then CHAMFER_MIN(w)
_COLLINEAR_TOL = 1e-6  # mm: a joint this close to the line through the far ends merges
_ON_TRACK_TOL = 0.003  # mm: a defect point / baseline point this close is the same place
_OCT = math.cos(math.pi / 8)
_NM = 1_000_000  # KiCad stores coordinates in integer nanometres
_DIRS = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))

_REASON = {
    "non_octilinear": "non_octilinear",
    "ninety": "ninety_unfixable",
    # round 2 (R3, owner rule a): a T-junction is legal (the own router branches as T, M4 no
    # longer fails it) - only a Y with a 90-degree pair is still ripped (as before); R4
    # handles every Y-junction here
    "t_junctions": "ninety_unfixable",
    "acute": "acute",
    "off_angle": "off_angle",
    "micro_jogs": "micro_jog",
    "short_chamfers": "short_chamfer",
}
_OTHER_FAILS = ("acute", "off_angle", "non_octilinear", "micro_jogs", "short_chamfers")


def _fails(rep: BendReport, field: str) -> Sequence[Point]:
    """`rep`'s points of `field`; for "t_junctions" only the Y-shaped ones (R3: a T is
    legal; a Y with a 90-degree pair was ripped before round 2 and still is)."""
    if field != "t_junctions":
        return tuple(getattr(rep, field))
    ys = rep.y_junctions
    return tuple(p for p in rep.t_junctions if any(math.dist(p, y) <= _JOIN_TOL for y in ys))


@dataclass(frozen=True)
class PostResult:
    replace: tuple[tuple[str, tuple[Track, ...]], ...]  # (old kiid, new tracks); () = delete
    rip: tuple[str, ...]  # connection keys "{net}:{refA}.{padA}-{refB}.{padB}"
    reasons: tuple[tuple[str, str], ...] = ()  # (key, reason)
    merged: int = 0
    chamfered: int = 0


# ---- small geometry ------------------------------------------------------------------------


def _tkey(t: Track) -> tuple[object, ...]:
    """KIID-free sort key (KIID last, as a tie-break only)."""
    a, b = sorted((t.start, t.end))
    return (t.net, t.layer, t.width, a, b, t.kiid)


def _dup_key(t: Track) -> tuple[object, ...]:
    a, b = sorted(
        ((round(t.start[0], 6), round(t.start[1], 6)), (round(t.end[0], 6), round(t.end[1], 6)))
    )
    return (t.net, t.layer, t.width, a, b)


def _len(t: Track) -> float:
    return math.dist(t.start, t.end)


def _unit(a: Point, b: Point) -> Point:
    d = math.dist(a, b)
    return ((b[0] - a[0]) / d, (b[1] - a[1]) / d)


def _pt_seg(p: Point, a: Point, b: Point) -> float:
    vx, vy = b[0] - a[0], b[1] - a[1]
    ll = vx * vx + vy * vy
    if ll <= 0.0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / ll))
    return math.dist(p, (a[0] + t * vx, a[1] + t * vy))


def _pt_line(p: Point, a: Point, b: Point) -> float:
    """Distance of `p` from the line through a and b (a != b)."""
    d = math.dist(a, b)
    return abs((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])) / d


def _nm(p: Point) -> tuple[int, int]:
    return (round(p[0] * _NM), round(p[1] * _NM))


def _octi_dir(a: Point, b: Point) -> tuple[int, int]:
    """The 0/45/90/... degree direction nearest to a -> b as an integer step."""
    k = round(math.atan2(b[1] - a[1], b[0] - a[0]) / (math.pi / 4)) % 8
    return _DIRS[k]


def _offset_nm(p: tuple[int, int], d: tuple[int, int], a: float) -> Point:
    """The point at least `a` mm from the nm-grid point `p` along the octilinear direction
    `d`, on the nm grid: each axis step rounded up to whole nanometres (equal on both axes
    of a diagonal), so a chamfer between two such points is exactly octilinear after
    KiCad's rounding and never shorter than asked."""
    step = math.ceil((a / math.sqrt(2.0) if d[0] and d[1] else a) * _NM - 1e-6)
    return ((p[0] + d[0] * step) / _NM, (p[1] + d[1] * step) / _NM)


def _octagon(c: Point, r: float) -> list[Point]:
    """Regular octagon circumscribing the circle (c, r); flats face 0/45/90/... degrees."""
    rr = r / _OCT
    return [
        (
            c[0] + rr * math.cos(math.pi / 8 + k * math.pi / 4),
            c[1] + rr * math.sin(math.pi / 8 + k * math.pi / 4),
        )
        for k in range(8)
    ]


def _capsule(pts: Sequence[Point], r: float) -> list[Point]:
    """Convex polygon covering every point within `r` of the hull of `pts`."""
    return convex_hull([q for p in pts for q in _octagon(p, r)])


def _bbox(poly: Sequence[Point]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (min(xs), min(ys), max(xs), max(ys))


def _near(p: Point, q: Point) -> bool:
    return math.dist(p, q) <= _JOIN_TOL


def _conn_key(net: str, a: Pad, b: Pad) -> str:
    a, b = sorted((a, b), key=lambda p: (natural_key(p.ref), natural_key(p.number)))
    return f"{net}:{a.ref}.{a.number}-{b.ref}.{b.number}"


def _clearance(model: BoardModel, net: str) -> float:
    rules = dict(model.rules)
    out = float(rules.get("min_clearance", 0.0))
    classes = {c.name: c for c in model.net_classes}
    name = dict(model.net_class_of).get(net)
    if name is not None:
        for part in name.split(","):
            cls = classes.get(part.strip())
            if cls is not None:
                out = max(out, cls.clearance)
    return out


# ---- obstacles -----------------------------------------------------------------------------


class _Obstacles:
    """Other-net copper of one layer (pads, tracks, arcs, vias; diagonals placed so far),
    each with its net's clearance (`_clearance`). Polygons are built lazily, only for items
    whose bounding box is hit."""

    def __init__(self, model: BoardModel, tracks: Sequence[Track], layer: str) -> None:
        self.model = model
        self.grid = _Grid()
        self.items: list[tuple[str, float, Callable[[], list[Point]]]] = []
        self.polys: dict[int, list[Point]] = {}
        self.clr: dict[str, float] = {}
        self.max_clr = 0.0
        for part in model.parts:
            for q in part.pads:
                if _pad_on_layer(q, layer):
                    r = math.hypot(q.w, q.h) / 2
                    self._add(q.net, _grow(((q.x, q.y),), r), partial(_pad_poly, q))
        for t in tracks:
            if t.layer == layer:
                ends = (t.start, t.end)
                self._add(
                    t.net, _grow(ends, t.width / 2 / _OCT), partial(_capsule, ends, t.width / 2)
                )
        for arc in model.arcs:
            if arc.layer == layer:
                pts = _arc_points(arc.start, arc.mid, arc.end)
                self._add(
                    arc.net, _grow(pts, arc.width / 2 / _OCT), partial(_capsule, pts, arc.width / 2)
                )
        for v in model.vias:
            self._add(
                v.net,
                _grow((v.at,), v.diameter / 2 / _OCT),
                partial(_octagon, v.at, v.diameter / 2),
            )

    def clearance(self, net: str) -> float:
        c = self.clr.get(net)
        if c is None:
            c = self.clr[net] = _clearance(self.model, net)
        return c

    def _add(
        self, net: str, box: tuple[float, float, float, float], make: Callable[[], list[Point]]
    ) -> None:
        c = self.clearance(net)
        self.max_clr = max(self.max_clr, c)
        self.grid.add(len(self.items), box)
        self.items.append((net, c, make))

    def add(self, net: str, poly: list[Point]) -> None:
        self.polys[len(self.items)] = poly
        self._add(net, _bbox(poly), lambda: poly)

    def blocked(self, net: str, ends: tuple[Point, Point], half: float) -> bool:
        """The segment `ends` (half width `half`) of `net`, inflated per obstacle by the
        larger of both nets' clearances, overlaps some other-net item."""
        own = self.clearance(net)
        caps: dict[float, list[Point]] = {}
        for i in self.grid.query(_grow(ends, (half + max(own, self.max_clr)) / _OCT)):
            other, oc, make = self.items[i]
            if other == net:
                continue
            r = half + max(own, oc)
            poly = caps.get(r)
            if poly is None:
                poly = caps[r] = _capsule(ends, r)
            opoly = self.polys.get(i)
            if opoly is None:
                opoly = self.polys[i] = make()
            if polygons_overlap(poly, opoly):
                return True
        return False


def _grow(pts: Sequence[Point], r: float) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = _bbox(pts)
    return (x0 - r, y0 - r, x1 + r, y1 + r)


# ---- step 1: drop and merge ----------------------------------------------------------------


def _line_cross(a: Point, b: Point, c: Point, d: Point) -> Point | None:
    """Intersection of the lines ab and cd (None when (nearly) parallel)."""
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    den = rx * sy - ry * sx
    if abs(den) <= 1e-9 * math.hypot(rx, ry) * math.hypot(sx, sy):
        return None
    t = ((c[0] - a[0]) * sy - (c[1] - a[1]) * sx) / den
    return (a[0] + t * rx, a[1] + t * ry)


def _on_line(x: Point, far: Point, near: Point) -> bool:
    """`x` continues the track far -> near: within 1 nm of its line, on near's side of far
    and at least the join tolerance from far (so the track keeps its direction)."""
    d = math.dist(far, near)
    ux, uy = (near[0] - far[0]) / d, (near[1] - far[1]) / d
    vx, vy = x[0] - far[0], x[1] - far[1]
    return abs(ux * vy - uy * vx) <= 1e-6 and ux * vx + uy * vy >= _JOIN_TOL


def _end_clusters(pts: Sequence[Point]) -> list[list[int]]:
    """Indices of `pts` grouped by chains of points within the join tolerance of each other
    (the points `geom.polylines` may snap into one node, depending on track order)."""
    root = list(range(len(pts)))

    def find(i: int) -> int:
        while root[i] != i:
            root[i] = root[root[i]]
            i = root[i]
        return i

    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, q in enumerate(pts):
        kx, ky = math.floor(q[0] / _JOIN_TOL), math.floor(q[1] / _JOIN_TOL)
        for gx in (kx - 1, kx, kx + 1):
            for gy in (ky - 1, ky, ky + 1):
                for j in cells.get((gx, gy), ()):
                    if math.dist(q, pts[j]) <= _JOIN_TOL:
                        root[find(i)] = find(j)
        cells[(kx, ky)].append(i)
    out: dict[int, list[int]] = defaultdict(list)
    for i in range(len(pts)):
        out[find(i)].append(i)
    return list(out.values())


def _close_gaps(
    model: BoardModel,
    untouched: Sequence[Track],
    live: list[Track],
    slots: dict[str, list[Track]],
) -> list[Track]:
    """Touched tracks shorter than the join tolerance are dropped like zero-length ones,
    and every cluster of track ends within the join tolerance of each other (chained) that
    such a piece or a gap left by a dropped track spreads over is closed: the touched
    tracks ending there get one shared end X on the nm grid, each moved along its own line
    (its direction unchanged). X is the cluster's untouched track end if it has one, else
    the first (sorted) neighbour end or pairwise intersection of the neighbours' lines that
    every neighbour's line passes through, within 2 x the join tolerance of the cluster and
    inside every same-net pad the cluster touches. A cluster at a via or an arc end, on a
    track's interior, with two distinct untouched ends or without such an X is left as it
    is (step 3 judges it). Returns the remaining touched tracks (moved ones replaced)."""
    by_nl: dict[tuple[str, str], list[Track]] = defaultdict(list)
    for t in (*untouched, *live):
        if _len(t) >= ZERO_LEN:
            by_nl[(t.net, t.layer)].append(t)
    live_ids = {id(t) for t in live}
    cur: dict[int, Track] = {}  # id(live track) -> its moved version
    drop: set[int] = set()
    pads_of: dict[str, dict[str, list[Pad]]] = {}  # layer -> net -> its pads on the layer
    for (net, layer), tracks in sorted(by_nl.items()):
        if not any(id(t) in live_ids for t in tracks):
            continue
        ends = [(q, k) for k, t in enumerate(tracks) for q in (t.start, t.end)]
        clusters = _end_clusters([q for q, _ in ends])
        todo = []
        for members in clusters:
            pts = {ends[i][0] for i in members}
            ks = sorted({ends[i][1] for i in members})
            if not any(id(tracks[k]) in live_ids for k in ks):
                continue
            tiny = [k for k in ks if id(tracks[k]) in live_ids and _len(tracks[k]) < _JOIN_TOL]
            if len(pts) > 1 or tiny:
                todo.append((members, pts, tiny))
        if not todo:
            continue
        if layer not in pads_of:
            pads_of[layer] = defaultdict(list)
            for p in model.parts:
                for q in p.pads:
                    if _pad_on_layer(q, layer):
                        pads_of[layer][q.net].append(q)
        near = _Near(pads_of[layer].get(net, []), [v for v in model.vias if v.net == net])
        arc_ends = [
            q for a in model.arcs if a.net == net and a.layer == layer for q in (a.start, a.end)
        ]
        grid = _Grid()
        for k, t in enumerate(tracks):
            grid.add(k, _bbox((t.start, t.end)))
        for members, pts, tiny in todo:
            box = _grow(list(pts), _JOIN_TOL)
            if any(near.at_via(q) for q in pts):
                continue
            if any(math.dist(a, q) <= _JOIN_TOL for a in arc_ends for q in pts):
                continue
            if any(
                _interior_t(q, tracks[k].start, tracks[k].end) is not None
                for k in grid.query(box)
                for q in pts
            ):
                continue
            fixed = {ends[i][0] for i in members if id(tracks[ends[i][1]]) not in live_ids}
            moving = [
                (ends[i][1], i % 2)
                for i in members
                if id(tracks[ends[i][1]]) in live_ids and ends[i][1] not in tiny
            ]
            if len(fixed) > 1 or len({k for k, _ in moving}) != len(moving):
                continue
            lines = []
            for k, side in moving:
                t = cur.get(id(tracks[k]), tracks[k])
                near_end, far = (t.start, t.end) if side == 0 else (t.end, t.start)
                lines.append((far, near_end))
            if fixed:
                cands = sorted(fixed)
            else:
                raw = [n for _, n in lines]
                for (f1, n1), (f2, n2) in itertools.combinations(lines, 2):
                    x = _line_cross(f1, n1, f2, n2)
                    if x is not None:
                        raw.append(x)
                cands = sorted({(round(x[0] * _NM) / _NM, round(x[1] * _NM) / _NM) for x in raw})
            pads = [q for q in near.pads if any(_in_pad(p, q) for p in pts)]
            pick = next(
                (
                    x
                    for x in cands
                    if all(math.dist(x, p) <= 2 * _JOIN_TOL for p in pts)
                    and all(_in_pad(x, q) for q in pads)
                    and all(_on_line(x, f, n) for f, n in lines)
                ),
                None,
            )
            if pick is None:
                continue  # left as it is: step 3 rips it if the polyline fails
            drop.update(id(tracks[k]) for k in tiny)
            for k, side in moving:
                t = cur.get(id(tracks[k]), tracks[k])
                cur[id(tracks[k])] = replace(t, start=pick) if side == 0 else replace(t, end=pick)
    out = []
    for t in live:
        if id(t) in drop:
            slots[t.kiid] = []
            continue
        moved = cur.get(id(t), t)
        if moved != t:
            slots[t.kiid] = [replace(moved, kiid="", locked=False)]
        out.append(moved)
    return out


def _drop_and_merge(
    model: BoardModel,
    untouched: Sequence[Track],
    touched: Sequence[Track],
    slots: dict[str, list[Track]],
) -> int:
    seen = {_dup_key(t) for t in untouched}
    live: list[Track] = []
    for t in touched:
        k = _dup_key(t)
        if _len(t) < ZERO_LEN or k in seen:
            slots[t.kiid] = []
            continue
        seen.add(k)
        live.append(t)
    live = _close_gaps(model, untouched, live, slots)
    # endpoints of every track per (net, layer), snapped
    by_nl: dict[tuple[str, str], list[Track]] = defaultdict(list)
    for t in (*untouched, *live):
        if _len(t) >= ZERO_LEN:
            by_nl[(t.net, t.layer)].append(t)
    touched_ids = {id(t) for t in live}
    parent: dict[str, str] = {t.kiid: t.kiid for t in live}

    def find(k: str) -> str:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    joints: set[tuple[str, str, Point]] = set()  # merged joints: (net, layer, snapped point)
    snapped: dict[str, tuple[Point, Point]] = {}  # touched kiid -> its snapped (start, end)
    near: dict[str, _Near] = {}  # layer -> every pad on it and every via
    for (_net, layer), tracks in sorted(by_nl.items()):
        if not any(id(t) in touched_ids for t in tracks):
            continue
        if layer not in near:
            pads = [q for p in model.parts for q in p.pads if _pad_on_layer(q, layer)]
            near[layer] = _Near(pads, model.vias)
        items = near[layer]
        nodes = _Nodes()
        ends: dict[Point, list[Track]] = defaultdict(list)
        grid = _Grid()
        for i, t in enumerate(tracks):
            sa, sb = nodes.snap(t.start), nodes.snap(t.end)
            ends[sa].append(t)
            ends[sb].append(t)
            if id(t) in touched_ids:
                snapped[t.kiid] = (sa, sb)
            grid.add(i, _bbox((t.start, t.end)))
        for p in sorted(ends):
            pair = ends[p]
            if len(pair) != 2 or not all(id(t) in touched_ids for t in pair):
                continue
            t1, t2 = pair
            if t1 is t2 or t1.width != t2.width:
                continue
            f1 = t1.end if _near(t1.start, p) else t1.start
            f2 = t2.end if _near(t2.start, p) else t2.start
            span = math.dist(f1, f2)
            if span < ZERO_LEN or _pt_seg(p, f1, f2) > _COLLINEAR_TOL:
                continue
            if math.dist(f1, p) + math.dist(p, f2) > span + _COLLINEAR_TOL:
                continue
            box = (p[0] - _JOIN_TOL, p[1] - _JOIN_TOL, p[0] + _JOIN_TOL, p[1] + _JOIN_TOL)
            if any(
                _interior_t(p, tracks[i].start, tracks[i].end) is not None
                for i in grid.query(box)
                if tracks[i] is not t1 and tracks[i] is not t2
            ):
                continue
            if items.at_via(p) or items.in_pad(p):
                continue
            parent[find(t1.kiid)] = find(t2.kiid)
            joints.add((t1.net, t1.layer, p))
    groups: dict[str, list[Track]] = defaultdict(list)
    for t in live:
        groups[find(t.kiid)].append(t)
    merged = 0
    for chain in groups.values():
        if len(chain) < 2:
            continue
        # the chain's outer ends: the track ends that are no merge joint of the chain's own
        # net/layer (joints are snapped nodes of that group, so compare the snapped nodes;
        # another net's or layer's joint at the same place is not one)
        outer = [
            q
            for t in chain
            for q, sq in zip((t.start, t.end), snapped[t.kiid], strict=True)
            if (t.net, t.layer, sq) not in joints
        ]
        a, b = min(outer), max(outer)
        first = next(t for t in chain if a in (t.start, t.end))
        new = Track(
            kiid="",
            net=first.net,
            layer=first.layer,
            start=a,
            end=b,
            width=first.width,
            locked=False,
        )
        for t in chain:
            slots[t.kiid] = []
        slots[first.kiid] = [new]
        merged += len(chain) - 1
    return merged


# ---- step 2: chamfer -----------------------------------------------------------------------


def _candidates(w: float) -> list[float]:
    lo = CHAMFER_MIN(w)
    out = [f * w for f in CHAMFER_FACTORS if f * w >= lo]
    if not out or out[-1] != lo:
        out.append(lo)
    return out


def _window_ok(
    pts: Sequence[Point],
    subst: dict[int, tuple[Point, Point]],
    lo: int,
    hi: int,
    width: float,
    pad_ends: tuple[bool, bool],
    pending: set[Point],
) -> bool:
    """`analyse_bends` of pts[lo..hi] with the corners in `subst` chamfered: no failure but
    90-degree vertices at `pending` (corners not chamfered here); every piece >= ZERO_LEN."""
    win: list[Point] = []
    for i in range(lo, hi + 1):
        if i in subst and lo < i < hi:
            win.extend(subst[i])
        else:
            win.append(pts[i])
    if any(math.dist(p, q) < ZERO_LEN for p, q in itertools.pairwise(win)):
        return False
    r = analyse_bends(
        win, width, pad_ends=(pad_ends[0] and lo == 0, pad_ends[1] and hi == len(pts) - 1)
    )
    if any(getattr(r, f) for f in _OTHER_FAILS):
        return False
    return all(p in pending for p in r.ninety)


def _copper_ends(
    model: BoardModel, net: str, layer: str, tracks: Sequence[Track]
) -> list[tuple[Point, int]]:
    """Every end of same-net copper on `layer` as (point, id of its track or 0): the
    net/layer's tracks and arcs. `polylines` sees no arcs, so an arc end at a corner or on a
    leg is invisible to it; a chamfer must never cut copper away from under such an end."""
    out = [(q, id(t)) for t in tracks for q in (t.start, t.end)]
    out.extend(
        (q, 0)
        for arc in model.arcs
        if arc.net == net and arc.layer == layer
        for q in (arc.start, arc.end)
    )
    return out


class _Chamferer:
    def __init__(
        self, model: BoardModel, work: Sequence[Track], slots: dict[str, list[Track]]
    ) -> None:
        self.model = model
        self.work = work
        self.slots = slots
        self.slot_of = {id(s[0]): k for k, s in slots.items() if s}
        self.obstacles: dict[str, _Obstacles] = {}
        self.ends = _Grid()  # the current net/layer's track ends (indices into its tracks)
        self.cends: list[tuple[Point, int]] = []  # the net/layer's copper ends (_copper_ends)
        self.cgrid = _Grid()
        self.count = 0
        self.by_nl: dict[tuple[str, str], list[Track]] = defaultdict(list)
        for t in work:
            self.by_nl[(t.net, t.layer)].append(t)
        self.parts_of: dict[str, list[Part]] = defaultdict(list)
        for part in model.parts:
            for net in sorted({q.net for q in part.pads}):
                self.parts_of[net].append(part)

    def _obst(self, layer: str) -> _Obstacles:
        o = self.obstacles.get(layer)
        if o is None:
            o = self.obstacles[layer] = _Obstacles(self.model, self.work, layer)
        return o

    def run(self, net: str, layer: str) -> None:
        tracks = self.by_nl[(net, layer)]
        parts = self.parts_of.get(net, [])
        pads = [q for p in parts for q in p.pads if q.net == net and _pad_on_layer(q, layer)]
        self.ends = _Grid()
        for i, t in enumerate(tracks):
            for e in (t.start, t.end):
                self.ends.add(i, (e[0], e[1], e[0], e[1]))
        self.cends = _copper_ends(self.model, net, layer, tracks)
        self.cgrid = _Grid()
        for i, (q, _) in enumerate(self.cends):
            self.cgrid.add(i, (q[0], q[1], q[0], q[1]))
        # polylines() only reads the net's own tracks, pads and vias
        sub = replace(self.model, tracks=tuple(tracks), parts=tuple(parts))
        near = _Near(pads, ())  # the net's pads on the layer, bucketed once per net/layer
        for pts, width in polylines(sub, net, layer):
            ends = pad_entry_ends(pts, near.in_pad)
            r = analyse_bends(pts, width, pad_ends=ends)
            if not r.ninety or any(getattr(r, f) for f in _OTHER_FAILS):
                continue
            self._polyline(list(pts), width, ends, r, tracks, net, layer)

    def _strands(self, s: Point, p: Point, e: Point, legs: tuple[int, int]) -> bool:
        """Some copper end other than the two corner tracks' lies on the cut-away corner."""
        box = _grow((s, p, e), _JOIN_TOL)
        for i in self.cgrid.query(box):
            q, owner = self.cends[i]
            if owner in legs:
                continue
            if _pt_seg(q, s, p) <= _JOIN_TOL or _pt_seg(q, p, e) <= _JOIN_TOL:
                return True
        return False

    def _leg(self, tracks: Sequence[Track], p: Point, toward: Point) -> list[Track]:
        u = _unit(p, toward)
        out = []
        box = (p[0] - _JOIN_TOL, p[1] - _JOIN_TOL, p[0] + _JOIN_TOL, p[1] + _JOIN_TOL)
        for t in (tracks[i] for i in self.ends.query(box)):
            if _near(t.start, p) or _near(t.end, p):
                far = t.end if _near(t.start, p) else t.start
                if math.dist(far, p) > _JOIN_TOL:
                    v = _unit(p, far)
                    if u[0] * v[0] + u[1] * v[1] > 1 - 1e-6:
                        out.append(t)
        return out

    def _polyline(
        self,
        pts: list[Point],
        width: float,
        ends: tuple[bool, bool],
        r: BendReport,
        tracks: Sequence[Track],
        net: str,
        layer: str,
    ) -> None:
        ninety = set(r.ninety)
        n = len(pts)
        pending: set[Point] = set()
        raw: list[tuple[int, str, str]] = []
        for i in range(1, n - 1):
            if pts[i] not in ninety:
                continue
            legs = (self._leg(tracks, pts[i], pts[i - 1]), self._leg(tracks, pts[i], pts[i + 1]))
            if len(legs[0]) != 1 or len(legs[1]) != 1:
                return  # cannot map the corner to its tracks: leave it (ripped later)
            mine = [self.slot_of.get(id(leg[0])) for leg in legs]
            if mine[0] is None and mine[1] is None:
                pending.add(pts[i])  # untouched copper: not ours
                continue
            if mine[0] is None or mine[1] is None:
                return  # half touched: cannot chamfer without changing untouched copper
            raw.append((i, mine[0], mine[1]))
        if not raw:
            return
        obst = self._obst(layer)
        all_pending = pending | {pts[i] for i, _, _ in raw}
        cands = _candidates(width)
        opts: dict[tuple[int, int], tuple[Point, Point] | None] = {}
        pairs: dict[tuple[int, int, int], bool] = {}

        def option(k: int, j: int) -> tuple[Point, Point] | None:
            """Diagonal (start, end) of candidate j at corner k when it is feasible on its
            own (room on both corner tracks, no obstacle, local analyse_bends), else None."""
            key = (k, j)
            if key not in opts:
                i, k_in, k_out = raw[k]
                p = pts[i]
                t_in, t_out = self.slots[k_in][0], self.slots[k_out][0]
                # on the nm grid, offsets rounded up (B-C12 step 2): all checks on these
                pn = _nm(p)
                q = (pn[0] / _NM, pn[1] / _NM)
                a = cands[j] / math.sqrt(2.0)
                s = _offset_nm(pn, _octi_dir(p, pts[i - 1]), a)
                e = _offset_nm(pn, _octi_dir(p, pts[i + 1]), a)
                out: tuple[Point, Point] | None = None
                lo, hi = max(0, i - 2), min(n - 1, i + 2)
                if (
                    math.dist(q, s) <= min(_len(t_in), math.dist(p, pts[i - 1])) - ZERO_LEN
                    and math.dist(q, e) <= min(_len(t_out), math.dist(p, pts[i + 1])) - ZERO_LEN
                    and not self._strands(s, q, e, (id(t_in), id(t_out)))
                    and not obst.blocked(net, (s, e), width / 2)
                    and _window_ok(pts, {i: (s, e)}, lo, hi, width, ends, all_pending - {p})
                ):
                    out = (s, e)
                opts[key] = out
            return opts[key]

        def pair_ok(k: int, j: int, j2: int) -> bool:
            """Corner k (choice j) and the adjacent corner k + 1 (choice j2) together."""
            key = (k, j, j2)
            if key not in pairs:
                o1, o2 = option(k, j), option(k + 1, j2)
                i1, i2 = raw[k][0], raw[k + 1][0]
                ok = False
                if o1 is not None and o2 is not None:
                    lo, hi = max(0, i1 - 2), min(n - 1, i2 + 2)
                    pend = all_pending - {pts[i1], pts[i2]}
                    ok = _window_ok(pts, {i1: o1, i2: o2}, lo, hi, width, ends, pend)
                pairs[key] = ok
            return pairs[key]

        # only adjacent corners (sharing a leg) interact: solve each run of them on its own
        # by depth-first search in candidate order (largest c first, from the run's start);
        # a (corner, candidate) whose remainder of the run cannot be completed is marked
        # dead, so each run costs at most O(len x candidates^2) checks
        runs: list[list[int]] = []
        for k, (i, _, _) in enumerate(raw):
            if runs and raw[runs[-1][-1]][0] == i - 1:
                runs[-1].append(k)
            else:
                runs.append([k])
        nc = len(cands)
        choice: dict[int, int] = {}
        for run in runs:
            dead: set[tuple[int, int]] = set()
            path: list[int] = []
            start = 0
            while len(path) < len(run):
                k = run[len(path)]
                prev = path[-1] if path else -1
                pick = next(
                    (
                        j
                        for j in range(start, nc)
                        if (k, j) not in dead
                        and option(k, j) is not None
                        and (prev < 0 or pair_ok(k - 1, prev, j))
                    ),
                    None,
                )
                if pick is None:
                    if not path:
                        return  # no candidate sequence fits: leave the polyline (ripped)
                    dead.add((run[len(path) - 1], path[-1]))
                    start = path.pop() + 1
                    continue
                path.append(pick)
                start = 0
            choice.update(zip(run, path, strict=True))
        chosen: list[tuple[int, str, str, tuple[Point, Point]]] = []
        for k, (i, k_in, k_out) in enumerate(raw):
            o = option(k, choice[k])
            assert o is not None
            chosen.append((i, k_in, k_out, o))
        subst = {i: o for i, _, _, o in chosen}
        if not _window_ok(pts, subst, 0, n - 1, width, ends, pending):
            return
        for i, k_in, k_out, (s, e) in chosen:
            p = pts[i]
            self._trim(k_in, p, s)
            self._trim(k_out, p, e)
            diag = Track(kiid="", net=net, layer=layer, start=s, end=e, width=width, locked=False)
            self.slots[k_out].append(diag)
            obst.add(net, _capsule((s, e), width / 2))
            self.count += 1

    def _trim(self, slot: str, p: Point, q: Point) -> None:
        body = self.slots[slot][0]
        body = replace(body, start=q) if _near(body.start, p) else replace(body, end=q)
        self.slots[slot][0] = replace(body, kiid="", locked=False)


# ---- connections / routed paths ------------------------------------------------------------


_Node = tuple[object, ...]


class _Paths:
    """The M5 copper graph of one net (as `metrics._NetGraph`) with each edge's track."""

    def __init__(
        self, model: BoardModel, tracks: Sequence[tuple[int, Track]], pads: Sequence[Pad]
    ) -> None:
        net = pads[0].net if pads else ""
        self.adj: dict[_Node, list[tuple[_Node, float, int]]] = defaultdict(list)
        nodes: dict[str, _Nodes] = defaultdict(_Nodes)
        by_layer: dict[str, list[tuple[Point, Point, float, int]]] = defaultdict(list)
        for i, t in tracks:
            if _len(t) >= 1e-6:
                a, b = nodes[t.layer].snap(t.start), nodes[t.layer].snap(t.end)
                if a != b:
                    by_layer[t.layer].append((a, b, math.dist(a, b), i))
        for arc in model.arcs:
            if arc.net == net and net:
                a, b = nodes[arc.layer].snap(arc.start), nodes[arc.layer].snap(arc.end)
                if a != b:
                    pts = _arc_points(arc.start, arc.mid, arc.end)
                    length = sum(math.dist(p, q) for p, q in itertools.pairwise(pts))
                    by_layer[arc.layer].append((a, b, length, -1))
        vias = [v for v in model.vias if v.net == net and net]
        all_nodes: list[tuple[str, Point]] = []
        for layer, segs in sorted(by_layer.items()):
            pts_set = {p for a, b, _, _ in segs for p in (a, b)}
            pts_set |= {nodes[layer].snap(v.at) for v in vias}
            pts_set |= {nodes[layer].snap((q.x, q.y)) for q in pads if _pad_on_layer(q, layer)}
            plist = sorted(pts_set)
            grid = _Grid()
            for k, p in enumerate(plist):
                grid.add(k, (p[0], p[1], p[0], p[1]))
            for a, b, length, tid in segs:
                if tid < 0:
                    self._edge(("t", layer, a), ("t", layer, b), length, tid)
                    continue
                box = (
                    min(a[0], b[0]) - _JOIN_TOL,
                    min(a[1], b[1]) - _JOIN_TOL,
                    max(a[0], b[0]) + _JOIN_TOL,
                    max(a[1], b[1]) + _JOIN_TOL,
                )
                cuts = sorted(
                    (tp, plist[k])
                    for k in grid.query(box)
                    if (tp := _interior_t(plist[k], a, b)) is not None
                )
                chain = [a, *(p for _, p in cuts), b]
                for p, q in itertools.pairwise(chain):
                    self._edge(("t", layer, p), ("t", layer, q), math.dist(p, q), tid)
            all_nodes.extend((layer, p) for p in plist)
        node_grid = _Grid()
        for k, (_, p) in enumerate(all_nodes):
            node_grid.add(k, (p[0], p[1], p[0], p[1]))

        def around(c: Point, r: float) -> list[tuple[str, Point]]:
            return [all_nodes[k] for k in node_grid.query((c[0] - r, c[1] - r, c[0] + r, c[1] + r))]

        for k, v in enumerate(vias):
            r = max(v.diameter / 2, _JOIN_TOL)
            for layer, p in around(v.at, r):
                if math.dist(p, v.at) <= r:
                    self._edge(("v", k), ("t", layer, p), 0.0, -1)
        for pad in pads:
            reach = math.hypot(pad.w, pad.h) / 2 + _JOIN_TOL
            for layer, p in around((pad.x, pad.y), reach):
                if _pad_on_layer(pad, layer) and _in_pad(p, pad):
                    self._edge(self.pad_node(pad), ("t", layer, p), 0.0, -1)
            for k, v in enumerate(vias):
                if any(x.endswith(".Cu") for x in pad.layers) and _in_pad(v.at, pad):
                    self._edge(("v", k), self.pad_node(pad), 0.0, -1)

    @staticmethod
    def pad_node(p: Pad) -> _Node:
        return ("p", p.ref, p.number, p.x, p.y)

    def _edge(self, a: _Node, b: _Node, w: float, tid: int) -> None:
        self.adj[a].append((b, w, tid))
        self.adj[b].append((a, w, tid))

    def path(self, a: Pad, b: Pad) -> tuple[float, frozenset[int]] | None:
        """(length, track ids) of the shortest path from pad `a` to pad `b`."""
        src, dst = self.pad_node(a), self.pad_node(b)
        if src not in self.adj or dst not in self.adj:
            return None
        dist: dict[_Node, float] = {src: 0.0}
        prev: dict[_Node, tuple[_Node, int]] = {}
        heap: list[tuple[float, int, _Node]] = [(0.0, 0, src)]
        tie = itertools.count(1)
        while heap:
            d, _, node = heapq.heappop(heap)
            if node == dst:
                used: set[int] = set()
                while node in prev:
                    node, tid = prev[node]
                    if tid >= 0:
                        used.add(tid)
                return d, frozenset(used)
            if d > dist.get(node, math.inf):
                continue
            for nxt, w, tid in self.adj[node]:
                nd = d + w
                if nd < dist.get(nxt, math.inf) - 1e-12:
                    dist[nxt] = nd
                    prev[nxt] = (node, tid)
                    heapq.heappush(heap, (nd, next(tie), nxt))
        return None


@dataclass(frozen=True)
class _Conn:
    key: str
    a: Pad
    b: Pad
    length: float | None
    used: frozenset[int]


# ---- step 3: whole-polyline check ----------------------------------------------------------

_BEND_FIELDS = ("non_octilinear", "ninety", "acute", "off_angle", "micro_jogs", "short_chamfers")


def _nm_track(t: Track) -> Track:
    return replace(
        t,
        start=(round(t.start[0] * _NM) / _NM, round(t.start[1] * _NM) / _NM),
        end=(round(t.end[0] * _NM) / _NM, round(t.end[1] * _NM) / _NM),
    )


def _touches(grid: _Grid, segs: Sequence[tuple[Point, Point]], line: Sequence[Point]) -> bool:
    """Some segment of the polyline `line` overlaps touched copper: its midpoint lies on one
    of `segs` (the touched tracks, bucketed by bbox in `grid`) within the on-track
    tolerance. Per segment, not per track midpoint: `polylines` T-splits a track where
    another ends on its interior, so the pieces of one touched track can land on
    polylines that hold none of the touched tracks' midpoints."""
    for a, b in itertools.pairwise(line):
        m = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        for k in grid.query((m[0], m[1], m[0], m[1])):
            if _pt_seg(m, *segs[k]) <= _ON_TRACK_TOL:
                return True
    return False


def _bend_fails(
    tracks: Sequence[Track],
    pads: Sequence[Pad],
    vias: Sequence[Via],
    keep: Callable[[Sequence[Point]], bool] | None,
) -> dict[str, list[Point]]:
    """Failing points per `analyse_bends` field over the polylines of one net/layer's
    `tracks` (`geom.polylines`; pad ends as M4 / M7: an end in a same-net pad on the layer),
    only the polylines `keep` accepts (all when None)."""
    near = _Near(pads, vias)
    out: dict[str, list[Point]] = defaultdict(list)
    for pts, width in _walk_polylines(_graph(tracks, _centres(vias, pads)), near):
        if keep is not None and not keep(pts):
            continue
        r = analyse_bends(pts, width, pad_ends=pad_entry_ends(pts, near.in_pad))
        for f in _BEND_FIELDS:
            out[f].extend(getattr(r, f))
    return out


# ---- entry point ---------------------------------------------------------------------------


def postprocess(model: BoardModel, kiids: frozenset[str]) -> PostResult:
    """Merge, chamfer and judge the tracks `kiids` (B-C12; see the module docstring)."""
    touched = sorted((t for t in model.tracks if t.kiid in kiids), key=_tkey)
    untouched = sorted((t for t in model.tracks if t.kiid not in kiids), key=_tkey)
    orig = {t.kiid: t for t in touched}
    slots: dict[str, list[Track]] = {t.kiid: [t] for t in touched}
    merged = _drop_and_merge(model, untouched, touched, slots)

    # step 2: chamfer
    def work_tracks() -> list[Track]:
        return sorted([*untouched, *(t for k in sorted(slots) for t in slots[k])], key=_tkey)

    ch = _Chamferer(model, work_tracks(), slots)
    for net, layer in sorted({(s[0].net, s[0].layer) for s in slots.values() if s}):
        ch.run(net, layer)

    # step 3 + 4 on the result
    wt = work_tracks()
    new_ids = {id(t) for s in slots.values() for t in s}
    mine = {i for i, t in enumerate(wt) if id(t) in new_ids}
    work = replace(model, tracks=tuple(wt))
    nets = sorted({wt[i].net for i in mine if wt[i].net})
    pads_of: dict[str, list[Pad]] = defaultdict(list)
    for p in model.parts:
        for q in p.pads:
            pads_of[q.net].append(q)
    by_net: dict[str, list[tuple[int, Track]]] = defaultdict(list)
    for i, t in enumerate(wt):
        by_net[t.net].append((i, t))
    conns: dict[str, list[_Conn]] = {}
    reasons: set[tuple[str, str]] = set()
    for net in nets:
        pads = sorted(pads_of.get(net, []), key=_pad_key)  # = metrics.connections(work, net)
        graph = _Paths(work, by_net[net], pads)
        before: _Paths | None = None
        out = []
        for a, b in _mst(pads):
            hit = graph.path(a, b)
            if hit is None:  # safety net: copper we changed must not cut a routed connection
                if before is None:
                    own = [(i, t) for i, t in enumerate(model.tracks) if t.net == net]
                    before = _Paths(model, own, pads)
                if before.path(a, b) is not None:
                    reasons.add((_conn_key(net, a, b), "disconnected"))
            out.append(
                _Conn(
                    _conn_key(net, a, b),
                    a,
                    b,
                    hit[0] if hit else None,
                    hit[1] if hit else frozenset(),
                )
            )
        conns[net] = out

    def rip(net: str, layer: str, tids: set[int], why: str) -> None:
        cs = conns.get(net, [])
        hit = [c for c in cs if c.used & tids]
        if not hit:
            hit = [c for c in cs if any(wt[i].layer == layer for i in c.used)]
        if not hit:
            hit = cs
        reasons.update((c.key, why) for c in hit)

    index = TrackIndex(work)
    by_nl: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, t in enumerate(wt):
        by_nl[(t.net, t.layer)].append(i)
    tol = _ON_TRACK_TOL
    for net, layer in sorted({(wt[i].net, wt[i].layer) for i in mine}):
        ids = by_nl[(net, layer)]
        near_grid = _Grid()  # the net/layer's tracks by bbox: which may pass near a defect
        for i in ids:
            near_grid.add(i, _bbox((wt[i].start, wt[i].end)))
        rep = analyse_tracks(replace(work, tracks=tuple(wt[i] for i in ids), arcs=()), index=index)
        # the same net/layer without the touched copper: a defect found there too is not ours
        rest = tuple(wt[i] for i in ids if i not in mine)
        base = analyse_tracks(replace(work, tracks=rest, arcs=()), index=index) if rest else None
        for field, why in _REASON.items():
            old: Sequence[Point] = _fails(base, field) if base is not None else ()
            old_grid = _Grid()  # the baseline's failing points of this kind, bucketed
            for k, o in enumerate(old):
                old_grid.add(k, (o[0], o[1], o[0], o[1]))
            for at in _fails(rep, field):
                box = (at[0] - tol, at[1] - tol, at[0] + tol, at[1] + tol)
                if any(math.dist(at, old[k]) <= tol for k in old_grid.query(box)):
                    continue
                tids = {
                    i
                    for i in near_grid.query(box)
                    if _pt_seg(at, wt[i].start, wt[i].end) <= _ON_TRACK_TOL
                }
                rip(net, layer, tids, why)
        # safety net (M4 / M7 as KiCad stores it): every polyline of the nm-rounded result
        # that contains touched copper, analysed as a whole - a piece or gap below the join
        # tolerance can make the walked polyline differ from its raw tracks (e.g. hide a
        # 90-degree vertex in a slightly non-octilinear segment)
        pads = index.layer(layer)[1].get(net, [])
        vias = index.vias_of.get(net, [])
        rounded = [_nm_track(wt[i]) for i in ids]
        own_grid = _Grid()  # the touched (rounded) tracks by bbox, grown by the tolerance
        own_segs: list[tuple[Point, Point]] = []
        for i, t in zip(ids, rounded, strict=True):
            if i in mine:
                own_grid.add(len(own_segs), _grow((t.start, t.end), _ON_TRACK_TOL))
                own_segs.append((t.start, t.end))

        # ends within the join tolerance of each other but not on one point (a piece or
        # gap step 1 could not close): `polylines` snaps them into nodes in track order, so
        # the walked geometry depends on that order - a defect unless it cannot matter:
        # every cluster point on the line of every track (>= the join tolerance) ending
        # there (a collinear gap), or the cluster one node in any order (diameter <= the
        # join tolerance) that turns none of those tracks by more than a quarter of the
        # angle tolerance (so the polylines checked below hold in every order)
        by_id = dict(zip(ids, rounded, strict=True))
        end_pts = [(q, i) for i, t in by_id.items() for q in (t.start, t.end)]
        for members in _end_clusters([q for q, _ in end_pts]):
            spread = {end_pts[k][0] for k in members}
            owners = {end_pts[k][1] for k in members}
            if len(spread) < 2 or not owners & mine:
                continue
            lines = [by_id[i] for i in owners if _len(by_id[i]) >= _JOIN_TOL]
            if all(_pt_line(q, t.start, t.end) <= 1e-6 for t in lines for q in spread):
                continue
            turn = math.radians(ANGLE_TOL_DEG / 4)
            if all(
                math.dist(u, v) <= _JOIN_TOL for u, v in itertools.combinations(spread, 2)
            ) and all(
                _pt_line(q, t.start, t.end) <= math.tan(turn) * (_len(t) - _JOIN_TOL)
                for t in lines
                for q in spread
            ):
                continue
            rip(net, layer, owners, "non_octilinear")
        got = _bend_fails(rounded, pads, vias, partial(_touches, own_grid, own_segs))
        base_rest = [t for i, t in zip(ids, rounded, strict=True) if i not in mine]
        has_fails = any(got.values())
        old_fails = _bend_fails(base_rest, pads, vias, None) if base_rest and has_fails else {}
        for field, pts_new in got.items():
            old = old_fails.get(field, [])
            old_grid = _Grid()
            for k, o in enumerate(old):
                old_grid.add(k, (o[0], o[1], o[0], o[1]))
            for pt in pts_new:
                box = (pt[0] - tol, pt[1] - tol, pt[0] + tol, pt[1] + tol)
                if any(math.dist(pt, old[k]) <= tol for k in old_grid.query(box)):
                    continue
                tids = {
                    i
                    for i in near_grid.query(box)
                    if _pt_seg(pt, wt[i].start, wt[i].end) <= _ON_TRACK_TOL
                }
                rip(net, layer, tids, _REASON[field])

    outline = outline_polygon(model)
    diag = None
    if outline is not None:
        x0, y0, x1, y1 = _bbox(outline)
        diag = math.hypot(x1 - x0, y1 - y0)
    for net in sorted(set(nets) - _ground_nets(nets)):
        for c in conns[net]:
            if c.length is None:
                continue
            octi = octi_dist((c.a.x, c.a.y), (c.b.x, c.b.y))
            if octi < M5_MIN_MM:
                continue
            ratio, extra = c.length / octi, c.length - octi
            if ratio > HARD["M5_ratio"] and extra > HARD["M5_extra_mm"]:
                reasons.add((c.key, "detour"))
            if (
                diag is not None
                and c.length > REVIEW["M6_diag_frac"] * diag
                and ratio > REVIEW["M5_any"]
            ):
                reasons.add((c.key, "long_detour"))

    rep_out = tuple((k, tuple(slots[k])) for k in sorted(slots) if slots[k] != [orig[k]])
    return PostResult(
        replace=rep_out,
        rip=tuple(sorted({k for k, _ in reasons})),
        reasons=tuple(sorted(reasons)),
        merged=merged,
        chamfered=ch.count,
    )
