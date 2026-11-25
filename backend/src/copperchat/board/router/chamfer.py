"""Freerouting post-processor (B-C12, P6a): merge, chamfer, rip list. Pure, deterministic,
no kipy import; module-level `postprocess` runs in `run_cpu`.

Only the tracks whose KIIDs are given ("touched") change; every other copper item is an
obstacle only. Steps:

1. Drop zero-length touched tracks (< 0.001 mm) and exact duplicates (same net, layer,
   width, endpoints in either order; an untouched twin wins, else the smallest KIID), then
   merge collinear same-net/layer/width touched tracks that share an endpoint with no third
   item at it (another track end or interior on that net/layer, a via, a pad on the layer).
2. Every 90-degree vertex of a polyline (`geom.polylines`) whose two corner tracks are
   touched is chamfered by one 45-degree diagonal of length c from `(3w, 2.5w, 2w, 1.5w,
   CHAMFER_MIN(w))` (only c >= CHAMFER_MIN(w); first feasible wins). c is feasible when the
   offset a = c / sqrt 2 leaves >= 0.001 mm of both corner tracks, the diagonal inflated by
   w / 2 + clearance (max of the net's class clearance and `rules["min_clearance"]`)
   overlaps no pad, track, arc or via of another net (diagonals placed earlier included),
   and `analyse_bends` of the changed stretch reports no failure. Adjacent corners share a
   leg: their offsets are chosen jointly (depth-first over each run of adjacent corners,
   larger c preferred from the run's start), so the straight piece left between them
   satisfies the chamfer/jog rules. The whole new polyline is re-checked by
   `analyse_bends`; a polyline with any unfixable corner (or any other
   failure) is left unchanged and ripped in step 3 - never partly chamfered.
3. `analyse_tracks` on the result, per net and layer: every failing point (non-octilinear,
   90-degree, acute, off-angle, off-pad T-junction, micro-jog, too-short chamfer) that lies
   on a touched track rips connections with the reason `non_octilinear`, `ninety_unfixable`
   (90-degree corners and T-junctions), `acute`, `off_angle`, `micro_jog`,
   `short_chamfer`. Failures on untouched copper only are not ours and are ignored.
4. Per connection of a touched non-ground net (octilinear distance >= `M5_MIN_MM`, as M5):
   ratio > 2.0 and extra > 5 mm -> `detour`; routed length > 50 % of the outline's bbox
   diagonal and ratio > 1.5 -> `long_detour` (M6 alone never rips).

Rip attribution: a defect lying on touched tracks rips every connection (`metrics.
connections`, pads ordered by natural (ref, number)) whose shortest routed path (the M5
copper graph: track ends snapped, T-splits, vias and pads at 0 mm) uses one of those
tracks. When none does (e.g. a dangling stub), every connection of that net whose path
uses that layer is ripped; when none does either, every connection of the net. A net with
fewer than two pads has no connection and therefore no rip key.

`replace` semantics: merging N tracks emits `(first kiid, (merged,))` (first = the chain
track at its lexicographically smaller end) and `(other kiid, ())`; a chamfer emits
`(kiid_in, (shortened_in,))` and `(kiid_out, (shortened_out, diagonal))` in polyline
order (a track between two corners: its kiid -> (shortened at both ends, the diagonal of
the corner it starts at)); zero-length and duplicate tracks `(kiid, ())`. Every new or
changed `Track` has `kiid=""`, the corner's net, layer and width and `locked=False`. Only
changed KIIDs appear.
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
    CHAMFER_MIN,
    BendReport,
    TrackIndex,
    _Grid,
    _in_pad,
    _interior_t,
    _Near,
    _Nodes,
    _pad_on_layer,
    analyse_bends,
    analyse_tracks,
    convex_hull,
    octi_dist,
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
from copperchat.board.model import BoardModel, Pad, Part, Point, Track
from copperchat.kicad_tools.filtering import natural_key

__all__ = ["CHAMFER_FACTORS", "ZERO_LEN", "PostResult", "postprocess"]

ZERO_LEN = 0.001  # mm: shorter tracks are dropped; every remaining piece is at least this
CHAMFER_FACTORS = (3.0, 2.5, 2.0, 1.5)  # x width, then CHAMFER_MIN(w)
_COLLINEAR_TOL = 1e-6  # mm: a joint this close to the line through the far ends merges
_ON_TRACK_TOL = 0.003  # mm: a defect point this close to a touched track belongs to it
_OCT = math.cos(math.pi / 8)

_REASON = {
    "non_octilinear": "non_octilinear",
    "ninety": "ninety_unfixable",
    "t_junctions": "ninety_unfixable",
    "acute": "acute",
    "off_angle": "off_angle",
    "micro_jogs": "micro_jog",
    "short_chamfers": "short_chamfer",
}
_OTHER_FAILS = ("acute", "off_angle", "non_octilinear", "micro_jogs", "short_chamfers")


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


def _along(p: Point, u: Point, d: float) -> Point:
    return (p[0] + u[0] * d, p[1] + u[1] * d)


def _pt_seg(p: Point, a: Point, b: Point) -> float:
    vx, vy = b[0] - a[0], b[1] - a[1]
    ll = vx * vx + vy * vy
    if ll <= 0.0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / ll))
    return math.dist(p, (a[0] + t * vx, a[1] + t * vy))


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
    """Other-net copper of one layer (pads, tracks, arcs, vias; diagonals placed so far).
    Polygons are built lazily, only for items whose bounding box is hit."""

    def __init__(self, model: BoardModel, tracks: Sequence[Track], layer: str) -> None:
        self.grid = _Grid()
        self.items: list[tuple[str, Callable[[], list[Point]]]] = []
        self.polys: dict[int, list[Point]] = {}
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

    def _add(
        self, net: str, box: tuple[float, float, float, float], make: Callable[[], list[Point]]
    ) -> None:
        self.grid.add(len(self.items), box)
        self.items.append((net, make))

    def add(self, net: str, poly: list[Point]) -> None:
        self.polys[len(self.items)] = poly
        self._add(net, _bbox(poly), lambda: poly)

    def blocked(self, net: str, poly: list[Point]) -> bool:
        for i in self.grid.query(_bbox(poly)):
            other, make = self.items[i]
            if other == net:
                continue
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

    joints: dict[str, list[Point]] = defaultdict(list)  # kiid -> merged joint points
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
            ends[nodes.snap(t.start)].append(t)
            ends[nodes.snap(t.end)].append(t)
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
            joints[t1.kiid].append(p)
            joints[t2.kiid].append(p)
    groups: dict[str, list[Track]] = defaultdict(list)
    for t in live:
        groups[find(t.kiid)].append(t)
    merged = 0
    for chain in groups.values():
        if len(chain) < 2:
            continue
        pts = [q for t in chain for q in (t.start, t.end)]
        outer = [q for q in pts if not any(_near(q, j) for t in chain for j in joints[t.kiid])]
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
        clr = _clearance(self.model, net)
        self.ends = _Grid()
        for i, t in enumerate(tracks):
            for e in (t.start, t.end):
                self.ends.add(i, (e[0], e[1], e[0], e[1]))
        # polylines() only reads the net's own tracks, pads and vias
        sub = replace(self.model, tracks=tuple(tracks), parts=tuple(parts))
        for pts, width in polylines(sub, net, layer):
            ends = (any(_in_pad(pts[0], q) for q in pads), any(_in_pad(pts[-1], q) for q in pads))
            r = analyse_bends(pts, width, pad_ends=ends)
            if not r.ninety or any(getattr(r, f) for f in _OTHER_FAILS):
                continue
            self._polyline(list(pts), width, ends, r, tracks, net, layer, clr)

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
        clr: float,
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
                a = cands[j] / math.sqrt(2.0)
                room = min(_len(self.slots[k_in][0]), _len(self.slots[k_out][0])) - ZERO_LEN
                out: tuple[Point, Point] | None = None
                if a <= room:
                    s = _along(p, _unit(p, pts[i - 1]), a)
                    e = _along(p, _unit(p, pts[i + 1]), a)
                    lo, hi = max(0, i - 2), min(n - 1, i + 2)
                    if not obst.blocked(net, _capsule((s, e), width / 2 + clr)) and _window_ok(
                        pts, {i: (s, e)}, lo, hi, width, ends, all_pending - {p}
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
    for net in nets:
        pads = sorted(pads_of.get(net, []), key=_pad_key)  # = metrics.connections(work, net)
        graph = _Paths(work, by_net[net], pads)
        out = []
        for a, b in _mst(pads):
            hit = graph.path(a, b)
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

    reasons: set[tuple[str, str]] = set()

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
    for net, layer in sorted({(wt[i].net, wt[i].layer) for i in mine}):
        ids = by_nl[(net, layer)]
        rep = analyse_tracks(replace(work, tracks=tuple(wt[i] for i in ids), arcs=()), index=index)
        own = [i for i in ids if i in mine]
        for field, why in _REASON.items():
            for p in getattr(rep, field):
                tids = {i for i in own if _pt_seg(p, wt[i].start, wt[i].end) <= _ON_TRACK_TOL}
                if tids:
                    rip(net, layer, tids, why)

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
