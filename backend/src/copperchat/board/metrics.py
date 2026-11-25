"""Quality metrics M1-M14 (B-C7, spec §12). Pure: no kipy import (`DrcMarker` / `DrcResult`
are only type-checked; they are read by attribute).

Thresholds are the module constants `HARD` (owner, D2 — never tuned) and `REVIEW` (spec §12
values; may be tuned on fixtures, recorded in the plan's "As built"). Every metric returns a
`Metric` with a verdict (fail > review > pass; `n/a` when its inputs are missing) and at most
`MAX_OFFENDERS` offenders sorted by value (descending) then text, plus an `omitted` count.

Interpretation notes (where the plan leaves a choice):

* M1 `M1_EDGE_OVERHANG` (owner, 2026-11-25): a part listed in `board.md` → Placement edges
  with an edge may overhang only that edge — its courtyard is clipped to the inner side of
  the edge line (outline bounding box) and the clipped polygon must lie inside the outline
  (`placed_with_overhang`). M13 uses the same clip for "overhanging it".
* M3 parity: `not_in_schematic` / `extra_footprint` markers whose refs are all mounting holes
  (holes placed by `pcb_set_outline`) produce no count. A warning is accepted when its
  normalised type equals a `board.md` Checks row's type and the refs of its KIIDs (part
  KIIDs) are non-empty and a subset of the row's items.
* M5/M6 use the connections (`connections`) of nets that are neither ground nor supply.
* M11: a hole of a filled polygon whose boundary lies within the union of the discs
  (centre, via radius / pad half-diagonal + `ANTIPAD_MARGIN_MM`) of the vias / plated pads
  near it is their antipad (also merged antipads of close vias) or thermal-relief gap, not
  a void; other holes and the region between the outline and the fill's outer boundary are
  voids. Each plane layer uses only its own filled polygons (`Zone.filled_layers`); without
  per-layer data (old models) all polygons are used and the fill area is divided by the
  zone's layer count. Adjacent layers = the copper layers next to the plane layer in
  `model.copper_layers` order.
* M14 silk text bbox: axis-aligned (TextItem has no angle), `len(line) x size_x x
  SILK_CHAR_WIDTH` wide and `lines x size_y` high, centred on `at` (top-left for text
  boxes). Fiducials and test points are exempt from the paste rule (they are never pasted;
  KiCad's TestPoint_Pad_* have no paste by design).
"""

from __future__ import annotations

import heapq
import itertools
import math
import re
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, get_args

from copperchat.board.boardmd import BoardDoc, Edge
from copperchat.board.classify import Classification
from copperchat.board.geom import (
    _JOIN_TOL,
    BendReport,
    _in_pad,
    _interior_t,
    _Nodes,
    _pad_on_layer,
    _proper_cross,
    analyse_tracks,
    convex_hull,
    octi_dist,
    polygon_area,
    polygon_inside,
    polygons_overlap,
    rect_poly,
)
from copperchat.board.model import (
    ArcTrack,
    BoardModel,
    Pad,
    Part,
    Point,
    TextItem,
    Track,
    Via,
)
from copperchat.board.presets import FabPreset
from copperchat.kicad_tools.filtering import natural_key

if TYPE_CHECKING:
    from copperchat.kicad_tools.board.api import DrcMarker, DrcResult

__all__ = [
    "HARD",
    "M1_EDGE_OVERHANG",
    "MAX_OFFENDERS",
    "REVIEW",
    "Metric",
    "MetricId",
    "MetricsReport",
    "Offender",
    "Verdict",
    "compute_metrics",
    "connections",
    "outline_polygon",
    "placed_with_overhang",
    "routed_length",
]

MetricId = Literal[
    "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9", "M10", "M11", "M12", "M13", "M14"
]
Verdict = Literal["pass", "review", "fail", "n/a"]
METRIC_IDS: tuple[MetricId, ...] = get_args(MetricId)

# spec §12 verbatim; HARD are the owner's (D2) and are never tuned
HARD: Mapping[str, float] = MappingProxyType(
    {"M4_angle_tol_deg": 0.5, "M5_ratio": 2.0, "M5_extra_mm": 5.0, "M8_mm": 5.0}
)
REVIEW: Mapping[str, float] = MappingProxyType(
    {
        "M5_median": 1.15,
        "M5_any": 1.5,
        "M5_critical": 1.25,
        "M6_diag_frac": 0.5,
        "M7_bends_per_10mm": 1.0,
        "M8_mm": 3.0,
        "M9_in_cap_mm": 3.0,
        "M9_inductor_mm": 3.0,
        "M9_sw_copper_mm": 5.0,
        "M10_ratio": 3.0,
        "M11_fill": 0.85,
        "M12_mismatch_mm": 1.0,
    }
)
M1_EDGE_OVERHANG = True  # owner decision 2026-11-25 (index "Provisional" 2)

MAX_OFFENDERS = 20
ANTIPAD_MARGIN_MM = 1.0  # M11: antipad clearance / thermal gap allowance around one item
M5_MIN_MM = 0.5  # connections shorter than this (octilinear) have no detour ratio
M7_MIN_NET_MM = 1.0  # nets with less routed copper do not enter the bends-per-10-mm average
M12_WIDTH_TOL = 0.001
M13_EDGE_MM = 0.5
SILK_CHAR_WIDTH = 1.0  # x size_x per character (KiCad's stroke font advance ≈ size)
BUCKET_MM = 2.0
_EPS = 1e-6
_RULE_TOL = 1e-4  # board rules come from nm integers
_SILK = {"F.SilkS": "F.Mask", "B.SilkS": "B.Mask"}


@dataclass(frozen=True)
class Offender:
    what: str
    refs: tuple[str, ...]
    nets: tuple[str, ...]
    at: Point | None
    value: float | None


@dataclass(frozen=True)
class Metric:
    id: MetricId
    value: float | int | None
    verdict: Verdict
    hard: str | None
    review: str | None
    offenders: tuple[Offender, ...]
    omitted: int = 0


@dataclass(frozen=True)
class MetricsReport:
    metrics: tuple[Metric, ...]
    hard_failures: int
    reviews: int
    stage: Literal["placement", "routed"]


def _g(v: float) -> str:
    return f"{v:g}"


THRESHOLD_TEXT: Mapping[MetricId, tuple[str | None, str | None]] = MappingProxyType(
    {
        "M1": ("> 0", None),
        "M2": ("> 0", None),
        "M3": ("errors + unaccepted warnings + parity > 0", "accepted warnings listed"),
        "M4": (f"> 0 (off 0/45/90 by > {_g(HARD['M4_angle_tol_deg'])}°, 90°, acute, T)", None),
        "M5": (
            f"ratio > {HARD['M5_ratio']:.1f} and extra > {_g(HARD['M5_extra_mm'])} mm",
            f"median > {_g(REVIEW['M5_median'])}; any > {_g(REVIEW['M5_any'])}; "
            f"critical > {_g(REVIEW['M5_critical'])}",
        ),
        "M6": (None, f"> {_g(REVIEW['M6_diag_frac'])} x board diagonal"),
        "M7": (
            "micro-jogs + short chamfers > 0",
            f"bends/10 mm > {_g(REVIEW['M7_bends_per_10mm'])} average",
        ),
        "M8": (f"> {_g(HARD['M8_mm'])} mm (routed) or other side", f"> {_g(REVIEW['M8_mm'])} mm"),
        "M9": (
            None,
            f"in-cap > {_g(REVIEW['M9_in_cap_mm'])} mm; inductor > "
            f"{_g(REVIEW['M9_inductor_mm'])} mm; SW copper > {_g(REVIEW['M9_sw_copper_mm'])} mm",
        ),
        "M10": (None, f"hull/courtyards > {_g(REVIEW['M10_ratio'])}; intrusions > 0"),
        "M11": (None, f"fill < {_g(REVIEW['M11_fill'])}; void crossings > 0; zone not filled"),
        "M12": ("width < class width", f"pair mismatch > {_g(REVIEW['M12_mismatch_mm'])} mm"),
        "M13": ("> 0", None),
        "M14": ("> 0 without a custom-rule exception", "custom-rule exceptions listed"),
    }
)


# ---- small geometry helpers ----------------------------------------------------------------

BBox = tuple[float, float, float, float]  # x0, y0, x1, y1


def _bbox(pts: Iterable[Point], grow: float = 0.0) -> BBox:
    xs, ys = zip(*pts, strict=True)
    return (min(xs) - grow, min(ys) - grow, max(xs) + grow, max(ys) + grow)


def _bbox_hit(a: BBox, b: BBox) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _cross3(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _pt_seg(p: Point, a: Point, b: Point) -> float:
    vx, vy = b[0] - a[0], b[1] - a[1]
    ll = vx * vx + vy * vy
    if ll <= 0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / ll))
    return math.dist(p, (a[0] + t * vx, a[1] + t * vy))


def _on_seg(p: Point, a: Point, b: Point) -> bool:
    return _pt_seg(p, a, b) <= _EPS


def _seg_hit(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Segments ab and cd share at least one point (touching included)."""
    if _proper_cross(a, b, c, d):
        return True
    return _on_seg(c, a, b) or _on_seg(d, a, b) or _on_seg(a, c, d) or _on_seg(b, c, d)


def _seg_seg(a: Point, b: Point, c: Point, d: Point) -> float:
    if _seg_hit(a, b, c, d):
        return 0.0
    return min(_pt_seg(a, c, d), _pt_seg(b, c, d), _pt_seg(c, a, b), _pt_seg(d, a, b))


def _ring_edges(pts: Sequence[Point]) -> list[tuple[Point, Point]]:
    return [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]


def _circle(a: Point, m: Point, b: Point) -> tuple[float, float, float] | None:
    """Centre and radius of the circle through a, m, b; None when they are collinear."""
    d = 2 * (a[0] * (m[1] - b[1]) + m[0] * (b[1] - a[1]) + b[0] * (a[1] - m[1]))
    if abs(d) < 1e-12:
        return None
    sa, sm, sb = a[0] ** 2 + a[1] ** 2, m[0] ** 2 + m[1] ** 2, b[0] ** 2 + b[1] ** 2
    ux = (sa * (m[1] - b[1]) + sm * (b[1] - a[1]) + sb * (a[1] - m[1])) / d
    uy = (sa * (b[0] - m[0]) + sm * (a[0] - b[0]) + sb * (m[0] - a[0])) / d
    return ux, uy, math.dist((ux, uy), a)


def _arc_len(a: Point, m: Point, b: Point) -> float:
    """Length of the circular arc from a through m to b (two chords when collinear)."""
    circle = _circle(a, m, b)
    if circle is None:
        return math.dist(a, m) + math.dist(m, b)
    ux, uy, r = circle
    half = math.asin(min(1.0, math.dist(a, b) / (2 * r)))
    # the mid point on the centre's side of the chord means the long way round
    major = _cross3(a, b, m) * _cross3(a, b, (ux, uy)) > 0
    return r * (2 * math.pi - 2 * half if major else 2 * half)


def _pad_poly(p: Pad) -> list[Point]:
    """The pad rectangle (w x h about its centre, rotated like `geom._in_pad`)."""
    t = math.radians(p.rot)
    c, s = math.cos(t), math.sin(t)
    out: list[Point] = []
    for lx, ly in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        x, y = lx * p.w / 2, ly * p.h / 2
        # inverse of _in_pad's local transform (lx = dx c - dy s, ly = dx s + dy c)
        out.append((p.x + x * c + y * s, p.y - x * s + y * c))
    return out


def _copper(p: Pad) -> bool:
    return any(layer.endswith(".Cu") for layer in p.layers)


class _Grid:
    """Uniform bucket grid (cell `BUCKET_MM`) of item ids keyed by their bounding boxes."""

    def __init__(self, cell: float = BUCKET_MM) -> None:
        self.cell = cell
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)

    def _keys(self, b: BBox) -> Iterator[tuple[int, int]]:
        c = self.cell
        for gx in range(math.floor(b[0] / c), math.floor(b[2] / c) + 1):
            for gy in range(math.floor(b[1] / c), math.floor(b[3] / c) + 1):
                yield gx, gy

    def add(self, item: int, b: BBox) -> None:
        for k in self._keys(b):
            self.cells[k].append(item)

    def query(self, b: BBox) -> list[int]:
        seen: set[int] = set()
        for k in self._keys(b):
            seen.update(self.cells.get(k, ()))
        return sorted(seen)


class _Ring:
    """A closed ring with a y-band index for fast even-odd point tests."""

    def __init__(self, pts: Sequence[Point], band: float = BUCKET_MM) -> None:
        self.pts = tuple(pts)
        self.bbox = _bbox(self.pts)
        self.band = band
        self.bands: dict[int, list[tuple[Point, Point]]] = defaultdict(list)
        for a, b in _ring_edges(self.pts):
            lo, hi = min(a[1], b[1]), max(a[1], b[1])
            for k in range(math.floor(lo / band), math.floor(hi / band) + 1):
                self.bands[k].append((a, b))

    def contains(self, p: Point) -> bool:
        x0, y0, x1, y1 = self.bbox
        if not (x0 - _EPS <= p[0] <= x1 + _EPS and y0 - _EPS <= p[1] <= y1 + _EPS):
            return False
        x, y = p
        inside = False
        for (ax, ay), (bx, by) in self.bands.get(math.floor(y / self.band), ()):
            if _on_seg(p, (ax, ay), (bx, by)):
                return True
            if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
                inside = not inside
        return inside


# ---- outline -------------------------------------------------------------------------------


def outline_polygon(model: BoardModel) -> list[Point] | None:
    """The board outline as one polygon (rounded rects with 8 segments per corner); None
    without an outline."""
    o = model.outline
    if o.kind == "rect":
        return rect_poly(o.x, o.y, o.w, o.h, o.corner_radius)
    if o.kind == "other" and len(o.polygon) >= 3:
        return list(o.polygon)
    return None


def _clip_half(pts: Sequence[Point], axis: int, bound: float, keep_ge: bool) -> list[Point]:
    """Sutherland-Hodgman clip of `pts` to the half-plane `p[axis] >= bound` (or `<=`)."""

    def inside(p: Point) -> bool:
        return p[axis] >= bound - _EPS if keep_ge else p[axis] <= bound + _EPS

    def cut(a: Point, b: Point) -> Point:
        t = (bound - a[axis]) / (b[axis] - a[axis])
        q = [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])]
        q[axis] = bound
        return (q[0], q[1])

    out: list[Point] = []
    for i, cur in enumerate(pts):
        prev = pts[i - 1]
        if inside(cur):
            if not inside(prev):
                out.append(cut(prev, cur))
            out.append(cur)
        elif inside(prev):
            out.append(cut(prev, cur))
    return out


def placed_with_overhang(model: BoardModel, part: Part, edge: Edge) -> bool:
    """`part`'s courtyard, clipped to the inner side of the outline's `edge` line (outline
    bounding box), lies inside the outline (boundary contact allowed): the part may overhang
    that one edge only (M1 with `M1_EDGE_OVERHANG`, M13)."""
    poly = outline_polygon(model)
    if poly is None or len(part.courtyard) < 3:
        return False
    x0, y0, x1, y1 = _bbox(poly)
    axis, bound, keep_ge = {
        "top": (1, y0, True),
        "bottom": (1, y1, False),
        "left": (0, x0, True),
        "right": (0, x1, False),
    }[edge]
    clipped = _clip_half(part.courtyard, axis, bound, keep_ge)
    if len(clipped) < 3 or polygon_area(clipped) <= _EPS:
        return False
    return polygon_inside(clipped, poly)


# ---- connections and routed length ---------------------------------------------------------


def _pad_key(p: Pad) -> tuple[object, ...]:
    return (natural_key(p.ref), natural_key(p.number), p.x, p.y)


def _net_pads(model: BoardModel, net: str) -> list[Pad]:
    return sorted((q for p in model.parts for q in p.pads if q.net == net), key=_pad_key)


def _mst(pads: Sequence[Pad]) -> list[tuple[Pad, Pad]]:
    """Prim's MST over `pads` with octilinear distances (ties: input order)."""
    if len(pads) < 2:
        return []
    pts = [(p.x, p.y) for p in pads]
    best = [math.inf] * len(pads)
    parent = [0] * len(pads)
    done = [False] * len(pads)
    done[0] = True
    for j in range(1, len(pads)):
        best[j] = octi_dist(pts[0], pts[j])
    out: list[tuple[Pad, Pad]] = []
    for _ in range(len(pads) - 1):
        k = min((j for j in range(len(pads)) if not done[j]), key=lambda j: best[j])
        done[k] = True
        out.append((pads[parent[k]], pads[k]))
        for j in range(len(pads)):
            if not done[j]:
                d = octi_dist(pts[k], pts[j])
                if d < best[j]:
                    best[j], parent[j] = d, k
    return out


def connections(model: BoardModel, net: str) -> list[tuple[Pad, Pad]]:
    """The connections of `net`: pad pairs of its ratsnest (octilinear MST over its pads)."""
    if not net:
        return []
    return _mst(_net_pads(model, net))


_Node = tuple[object, ...]


class _NetGraph:
    """The copper graph of one net: track ends snapped per layer (0.002 mm), tracks split
    where another end lies on their interior (T), vias join every layer at 0 cost, a pad
    joins every track end inside it at 0 cost."""

    def __init__(
        self,
        tracks: Sequence[Track],
        arcs: Sequence[ArcTrack],
        vias: Sequence[Via],
        pads: Sequence[Pad],
    ) -> None:
        """`tracks`, `arcs`, `vias` and `pads` are the net's own."""
        self.adj: dict[_Node, list[tuple[_Node, float]]] = defaultdict(list)
        by_layer: dict[str, list[tuple[Point, Point, float, bool]]] = defaultdict(list)
        nodes: dict[str, _Nodes] = defaultdict(_Nodes)
        for t in tracks:
            if math.dist(t.start, t.end) >= _EPS:
                a, b = nodes[t.layer].snap(t.start), nodes[t.layer].snap(t.end)
                if a != b:
                    by_layer[t.layer].append((a, b, math.dist(a, b), True))
        for arc in arcs:
            a, b = nodes[arc.layer].snap(arc.start), nodes[arc.layer].snap(arc.end)
            if a != b:
                by_layer[arc.layer].append((a, b, _arc_len(a, arc.mid, b), False))
        via_at = [v.at for v in vias]
        ends: dict[str, set[Point]] = {}
        for layer, segs in by_layer.items():
            # track ends and via centres split a straight track they lie on (a T)
            pts = {p for a, b, _, _ in segs for p in (a, b)}
            pts |= {nodes[layer].snap(v) for v in via_at}
            ends[layer] = pts
            grid = _Grid()
            plist = sorted(pts)
            for i, p in enumerate(plist):
                grid.add(i, (p[0], p[1], p[0], p[1]))
            for a, b, length, straight in segs:
                if straight:
                    cuts = []
                    for i in grid.query(_bbox((a, b), _JOIN_TOL)):
                        tp = _interior_t(plist[i], a, b)
                        if tp is not None:
                            cuts.append((tp, plist[i]))
                    chain = [a, *(p for _, p in sorted(cuts)), b]
                    for p, q in itertools.pairwise(chain):
                        self._edge(("t", layer, p), ("t", layer, q), math.dist(p, q))
                else:
                    self._edge(("t", layer, a), ("t", layer, b), length)
        all_nodes = [(layer, p) for layer, pts in ends.items() for p in sorted(pts)]
        grid = _Grid()
        for i, (_, p) in enumerate(all_nodes):
            grid.add(i, (p[0], p[1], p[0], p[1]))
        for k, v in enumerate(vias):
            r = max(v.diameter / 2, _JOIN_TOL)
            for i in grid.query(_bbox((v.at,), r)):
                layer, p = all_nodes[i]
                if math.dist(p, v.at) <= r:
                    self._edge(("v", k), ("t", layer, p), 0.0)
        for pad in pads:
            r = math.hypot(pad.w, pad.h) / 2 + _JOIN_TOL
            for i in grid.query(_bbox(((pad.x, pad.y),), r)):
                layer, p = all_nodes[i]
                if _pad_on_layer(pad, layer) and _in_pad(p, pad):
                    self._edge(self.pad_node(pad), ("t", layer, p), 0.0)

    @staticmethod
    def pad_node(p: Pad) -> _Node:
        return ("p", p.ref, p.number, p.x, p.y)

    def _edge(self, a: _Node, b: _Node, w: float) -> None:
        self.adj[a].append((b, w))
        self.adj[b].append((a, w))

    def length(self, a: Pad, b: Pad) -> float | None:
        src, dst = self.pad_node(a), self.pad_node(b)
        if src == dst:
            return 0.0
        if src not in self.adj or dst not in self.adj:
            return None
        dist: dict[_Node, float] = {src: 0.0}
        heap: list[tuple[float, int, _Node]] = [(0.0, 0, src)]
        tie = itertools.count(1)
        while heap:
            d, _, n = heapq.heappop(heap)
            if n == dst:
                return d
            if d > dist.get(n, math.inf):
                continue
            for m, w in self.adj[n]:
                nd = d + w
                if nd < dist.get(m, math.inf) - 1e-12:
                    dist[m] = nd
                    heapq.heappush(heap, (nd, next(tie), m))
        return None


def routed_length(model: BoardModel, a: Pad, b: Pad) -> float | None:
    """Shortest path between pads `a` and `b` through their net's track/via graph (vias
    0 mm); None when they are not connected by copper (or on different nets)."""
    if a.net != b.net or not a.net:
        return None
    net = a.net
    return _NetGraph(
        [t for t in model.tracks if t.net == net],
        [arc for arc in model.arcs if arc.net == net],
        [v for v in model.vias if v.net == net],
        _net_pads(model, net),
    ).length(a, b)


# ---- context -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Conn:
    net: str
    a: Pad
    b: Pad
    octi: float
    routed: float


class _Ctx:
    def __init__(
        self, model: BoardModel, cls: Classification, doc: BoardDoc, preset: FabPreset | None
    ) -> None:
        self.m = model
        self.cls = cls
        self.doc = doc
        self.preset = preset
        self.parts = {p.ref: p for p in model.parts}
        self.ref_of_kiid = {p.kiid: p.ref for p in model.parts}
        self.pads_by_net: dict[str, list[Pad]] = defaultdict(list)
        for p in model.parts:
            for q in p.pads:
                self.pads_by_net[q.net].append(q)
        for pads in self.pads_by_net.values():
            pads.sort(key=_pad_key)
        self.outline = outline_polygon(model)
        self.track_len: dict[str, float] = defaultdict(float)
        # each net's own items, grouped once (per-net work must not scan the whole board)
        self.tracks_by_net: dict[str, list[Track]] = defaultdict(list)
        self.arcs_by_net: dict[str, list[ArcTrack]] = defaultdict(list)
        self.vias_by_net: dict[str, list[Via]] = defaultdict(list)
        for t in model.tracks:
            self.track_len[t.net] += math.dist(t.start, t.end)
            self.tracks_by_net[t.net].append(t)
        for arc in model.arcs:
            self.track_len[arc.net] += _arc_len(arc.start, arc.mid, arc.end)
            self.arcs_by_net[arc.net].append(arc)
        for v in model.vias:
            self.vias_by_net[v.net].append(v)
        routed = any(n and self.kind(n) != "ground" for n in self.track_len)
        self.stage: Literal["placement", "routed"] = "routed" if routed else "placement"
        self._graphs: dict[str, _NetGraph] = {}
        self._bends: dict[str, BendReport] | None = None
        self._conns: list[_Conn] | None = None

    def kind(self, net: str) -> str:
        return self.cls.net_kind.get(net, "signal")

    def signal(self, net: str) -> bool:
        return bool(net) and self.kind(net) not in ("ground", "supply")

    def graph(self, net: str) -> _NetGraph:
        g = self._graphs.get(net)
        if g is None:
            g = self._graphs[net] = _NetGraph(
                self.tracks_by_net.get(net, []),
                self.arcs_by_net.get(net, []),
                self.vias_by_net.get(net, []),
                self.pads_by_net.get(net, []),
            )
        return g

    def path(self, a: Pad, b: Pad) -> tuple[float, bool]:
        """(length, routed): the routed length at stage `routed` when connected, else the
        octilinear estimate."""
        est = octi_dist((a.x, a.y), (b.x, b.y))
        if self.stage == "routed" and a.net == b.net and a.net:
            r = self.graph(a.net).length(a, b)
            if r is not None:
                return r, True
        return est, False

    def bends(self) -> dict[str, BendReport]:
        """`analyse_tracks` per net, each on a sub-model holding only the net's tracks/arcs and
        the parts/vias near them (the sum equals `analyse_tracks(model)`; pad and via tests
        only ever look at points of the net's own tracks)."""
        if self._bends is not None:
            return self._bends
        tracks, arcs = self.tracks_by_net, self.arcs_by_net
        # bounding boxes once, bucketed: each net only looks at the parts / vias near it
        parts = [p for p in self.m.parts if p.pads]
        part_boxes = [
            _bbox(((q.x, q.y) for q in p.pads), max(math.hypot(q.w, q.h) for q in p.pads))
            for p in parts
        ]
        via_boxes = [_bbox((v.at,), max(v.diameter, _JOIN_TOL)) for v in self.m.vias]
        part_grid, via_grid = _Grid(), _Grid()
        for i, b in enumerate(part_boxes):
            part_grid.add(i, b)
        for i, b in enumerate(via_boxes):
            via_grid.add(i, b)
        out: dict[str, BendReport] = {}
        for net in sorted(set(tracks) | set(arcs)):
            pts = [pt for t in tracks.get(net, ()) for pt in (t.start, t.end)]
            pts += [pt for a in arcs.get(net, ()) for pt in (a.start, a.mid, a.end)]
            box = _bbox(pts, 2 * _JOIN_TOL)
            sub = replace(
                self.m,
                tracks=tuple(tracks.get(net, ())),
                arcs=tuple(arcs.get(net, ())),
                parts=tuple(
                    parts[i] for i in part_grid.query(box) if _bbox_hit(part_boxes[i], box)
                ),
                vias=tuple(
                    self.m.vias[i] for i in via_grid.query(box) if _bbox_hit(via_boxes[i], box)
                ),
            )
            out[net] = analyse_tracks(sub)
        self._bends = out
        return out

    def conns(self) -> list[_Conn]:
        """Routed connections of every non-ground, non-supply net."""
        if self._conns is not None:
            return self._conns
        out: list[_Conn] = []
        for net in sorted(n for n in self.track_len if self.signal(n)):
            for a, b in _mst(self.pads_by_net.get(net, [])):
                r = self.graph(net).length(a, b)
                if r is not None:
                    out.append(_Conn(net, a, b, octi_dist((a.x, a.y), (b.x, b.y)), r))
        self._conns = out
        return out

    def refs(self, kiids: Iterable[str]) -> tuple[str, ...]:
        refs = {self.ref_of_kiid[k] for k in kiids if k in self.ref_of_kiid}
        return tuple(sorted(refs, key=natural_key))


# ---- metric assembly -----------------------------------------------------------------------


def _sort_key(o: Offender) -> tuple[object, ...]:
    return (o.value is None, -(o.value or 0.0), o.what, o.refs, o.nets)


def _metric(
    mid: MetricId,
    value: float | int | None,
    verdict: Verdict,
    offenders: Iterable[Offender] = (),
) -> Metric:
    hard, review = THRESHOLD_TEXT[mid]
    ordered = sorted(offenders, key=_sort_key)
    return Metric(
        id=mid,
        value=value,
        verdict=verdict,
        hard=hard,
        review=review,
        offenders=tuple(ordered[:MAX_OFFENDERS]),
        omitted=max(0, len(ordered) - MAX_OFFENDERS),
    )


def _verdict(fail: bool, review: bool = False) -> Verdict:
    return "fail" if fail else "review" if review else "pass"


def _r(v: float) -> float:
    return round(v, 4) + 0.0


def _pin(p: Pad) -> str:
    return f"{p.ref}.{p.number}"


# ---- M1 placement --------------------------------------------------------------------------


def _m1(c: _Ctx) -> Metric:
    if c.outline is None:
        return _metric("M1", None, "n/a")
    edges = {row.ref: row.edge for row in c.doc.edges if row.edge is not None}
    off: list[Offender] = []
    with_cy: list[Part] = []
    for p in c.m.parts:
        at = (p.x, p.y)
        if p.side not in c.doc.allowed_sides and any(q.smd for q in p.pads):
            allowed = "+".join(sorted(c.doc.allowed_sides))
            off.append(
                Offender(
                    f"smd_side: {p.ref} on {p.side}, allowed {allowed}", (p.ref,), (), at, None
                )
            )
        if len(p.courtyard) < 3:
            off.append(Offender(f"no_courtyard: {p.ref}", (p.ref,), (), at, None))
            continue
        with_cy.append(p)
        placed = c.m.placed(p)
        edge = edges.get(p.ref)
        if not placed and M1_EDGE_OVERHANG and edge is not None:
            placed = placed_with_overhang(c.m, p, edge)
        if not placed:
            off.append(Offender(f"not_placed: {p.ref} outside the outline", (p.ref,), (), at, None))
    for side in ("top", "bottom"):
        items = sorted(
            ((_bbox(p.courtyard), p) for p in with_cy if p.side == side), key=lambda i: i[0][0]
        )
        for i, (ba, pa) in enumerate(items):
            for bb, pb in items[i + 1 :]:
                if bb[0] > ba[2]:
                    break
                if _bbox_hit(ba, bb) and polygons_overlap(pa.courtyard, pb.courtyard):
                    refs = tuple(sorted((pa.ref, pb.ref), key=natural_key))
                    off.append(Offender(f"overlap: {refs[0]} / {refs[1]}", refs, (), None, None))
    return _metric("M1", len(off), _verdict(bool(off)), off)


# ---- M2 / M3 DRC ---------------------------------------------------------------------------


def _norm_type(t: str) -> str:
    return re.sub(r"\s+", "_", t.strip().casefold()).removeprefix("drce_")


def _m2(c: _Ctx, drc: DrcResult | None) -> Metric:
    if drc is None:
        return _metric("M2", None, "n/a")
    off = [
        Offender(f"unconnected: {mk.text}", c.refs(mk.kiids), (), mk.pos, None)
        for mk in drc.markers
        if _norm_type(mk.type).startswith("unconnected")
    ]
    return _metric("M2", drc.unconnected, _verdict(drc.unconnected > 0), off)


_HOLE_PARITY = frozenset({"not_in_schematic", "extra_footprint"})


def _m3(c: _Ctx, drc: DrcResult | None, parity: tuple[DrcMarker, ...] | None) -> Metric:
    if drc is None and parity is None:
        return _metric("M3", None, "n/a")
    rows = [(_norm_type(r.drc_type), frozenset(r.items)) for r in c.doc.accepted if r.items]
    off: list[Offender] = []
    count = accepted = 0
    for mk in drc.markers if drc is not None else ():
        t = _norm_type(mk.type)
        if t.startswith("unconnected") or mk.severity not in ("error", "warning"):
            continue
        refs = c.refs(mk.kiids)
        if (
            mk.severity == "warning"
            and refs
            and any(t == rt and set(refs) <= it for rt, it in rows)
        ):
            accepted += 1
            off.append(Offender(f"accepted {t}: {mk.text}", refs, (), mk.pos, None))
            continue
        count += 1
        off.append(Offender(f"{mk.severity} {t}: {mk.text}", refs, (), mk.pos, None))
    for mk in parity or ():
        t = _norm_type(mk.type)
        refs = c.refs(mk.kiids)
        if (
            t in _HOLE_PARITY
            and refs
            and all(c.cls.part_class.get(r) == "mounting_hole" for r in refs)
        ):
            continue
        count += 1
        off.append(Offender(f"parity {t}: {mk.text}", refs, (), mk.pos, None))
    return _metric("M3", count, _verdict(count > 0, accepted > 0), off)


# ---- M4 / M7 bends -------------------------------------------------------------------------

_M4_KINDS = (
    ("non_octilinear", "non_octilinear"),
    ("ninety", "ninety"),
    ("acute", "acute"),
    ("off_angle", "off_angle"),
    ("t_junctions", "t_junction"),
)


def _m4(c: _Ctx) -> Metric:
    off = [
        Offender(label, (), (net,), pt, None)
        for net, rep in c.bends().items()
        for attr, label in _M4_KINDS
        for pt in getattr(rep, attr)
    ]
    return _metric("M4", len(off), _verdict(bool(off)), off)


def _m7(c: _Ctx) -> Metric:
    bends = c.bends()
    off = [
        Offender(label, (), (net,), pt, None)
        for net, rep in bends.items()
        for attr, label in (("micro_jogs", "micro_jog"), ("short_chamfers", "short_chamfer"))
        for pt in getattr(rep, attr)
    ]
    hard = len(off)
    rates: dict[str, float] = {}
    for net, rep in bends.items():
        length = c.track_len.get(net, 0.0)
        if c.signal(net) and length >= M7_MIN_NET_MM:
            rates[net] = rep.bends * 10.0 / length
    limit = REVIEW["M7_bends_per_10mm"]
    review = bool(rates) and statistics.fmean(rates.values()) > limit
    if review:
        off += [
            Offender(f"bends {net}: {r:.2f} per 10 mm", (), (net,), None, _r(r))
            for net, r in rates.items()
            if r > limit
        ]
    return _metric("M7", hard, _verdict(hard > 0, review), off)


# ---- M5 / M6 connections -------------------------------------------------------------------


def _conn_text(k: _Conn) -> str:
    return f"{k.net} {_pin(k.a)}-{_pin(k.b)}"


def _m5(c: _Ctx) -> Metric:
    if c.stage == "placement":
        return _metric("M5", None, "n/a")
    conns = [k for k in c.conns() if k.octi >= M5_MIN_MM]
    if not conns:
        return _metric("M5", None, "pass")
    critical = set(c.cls.critical_nets)
    fail = review = False
    off: list[Offender] = []
    ratios = []
    for k in conns:
        ratio, extra = k.routed / k.octi, k.routed - k.octi
        ratios.append(ratio)
        hard = ratio > HARD["M5_ratio"] and extra > HARD["M5_extra_mm"]
        rev = ratio > REVIEW["M5_any"] or (k.net in critical and ratio > REVIEW["M5_critical"])
        fail |= hard
        review |= rev
        if hard or rev or ratio > REVIEW["M5_median"]:
            label = "detour_fail" if hard else "detour"
            what = f"{label} {_conn_text(k)}: ratio {ratio:.2f}, extra {extra:.2f} mm"
            off.append(Offender(what, (k.a.ref, k.b.ref), (k.net,), (k.a.x, k.a.y), _r(ratio)))
    median = statistics.median(ratios)
    review |= median > REVIEW["M5_median"]
    return _metric("M5", _r(median), _verdict(fail, review), off)


def _m6(c: _Ctx) -> Metric:
    if c.outline is None or c.stage == "placement":
        return _metric("M6", None, "n/a")
    x0, y0, x1, y1 = _bbox(c.outline)
    limit = REVIEW["M6_diag_frac"] * math.hypot(x1 - x0, y1 - y0)
    off = [
        Offender(
            f"long {_conn_text(k)}: {k.routed:.1f} mm",
            (k.a.ref, k.b.ref),
            (k.net,),
            (k.a.x, k.a.y),
            _r(k.routed),
        )
        for k in c.conns()
        if k.routed > limit
    ]
    return _metric("M6", len(off), _verdict(False, bool(off)), off)


# ---- M8 / M9 short paths -------------------------------------------------------------------


def _pads_on(part: Part | None, net: str) -> list[Pad]:
    return [] if part is None else sorted((q for q in part.pads if q.net == net), key=_pad_key)


def _nearest(a_pads: Sequence[Pad], b_pads: Sequence[Pad]) -> tuple[Pad, Pad] | None:
    pairs = [(a, b) for a in a_pads for b in b_pads]
    if not pairs:
        return None
    return min(pairs, key=lambda ab: octi_dist((ab[0].x, ab[0].y), (ab[1].x, ab[1].y)))


def _m8(c: _Ctx) -> Metric:
    items: list[tuple[str, Part, Part, Pad, Pad]] = []
    for d in c.cls.decoupling:
        cap, chip = c.parts.get(d.cap), c.parts.get(d.ic)
        sup = _pads_on(cap, d.supply_net)
        pin = [q for q in (chip.pads if chip else ()) if q.number == d.pin]
        if cap is not None and chip is not None and sup and pin:
            items.append(("decoupling", cap, chip, sup[0], pin[0]))
    for x in c.cls.crystals:
        xt, chip = c.parts.get(x.ref), c.parts.get(x.ic)
        for net in x.nets:
            pair = _nearest(_pads_on(xt, net), _pads_on(chip, net))
            if xt is not None and chip is not None and pair is not None:
                items.append(("crystal", xt, chip, pair[0], pair[1]))
    if not items:
        return _metric("M8", None, "n/a")
    off: list[Offender] = []
    fail = review = False
    worst = 0.0
    for kind, small, chip, a, b in items:
        length, routed = c.path(a, b)
        worst = max(worst, length)
        how = "routed" if routed else "estimate"
        base = f"{kind} {_pin(a)}->{_pin(b)}"
        refs, nets, at = (small.ref, chip.ref), (a.net,), (a.x, a.y)
        if small.side != chip.side:
            fail = True
            off.append(Offender(f"{base}: other side", refs, nets, at, _r(length)))
        if length > HARD["M8_mm"] and c.stage == "routed":
            fail = True
            off.append(Offender(f"{base}: {length:.2f} mm ({how})", refs, nets, at, _r(length)))
        elif length > REVIEW["M8_mm"]:
            review = True
            off.append(Offender(f"{base}: {length:.2f} mm ({how})", refs, nets, at, _r(length)))
    return _metric("M8", _r(worst), _verdict(fail, review), off)


def _m9(c: _Ctx) -> Metric:
    if not c.cls.switchers:
        return _metric("M9", None, "n/a")
    off: list[Offender] = []
    for s in c.cls.switchers:
        chip = c.parts.get(s.ic)
        if chip is None:
            continue
        vin_pins = _pads_on(chip, s.vin_net)
        for ref in s.in_caps:
            pair = _nearest(_pads_on(c.parts.get(ref), s.vin_net), vin_pins)
            if pair is None:
                continue
            length, _ = c.path(*pair)
            if length > REVIEW["M9_in_cap_mm"]:
                what = f"in_cap {_pin(pair[0])}->{_pin(pair[1])}: {length:.2f} mm"
                off.append(
                    Offender(what, (ref, s.ic), (s.vin_net,), (pair[0].x, pair[0].y), _r(length))
                )
        pair = _nearest(_pads_on(c.parts.get(s.inductor), s.sw_net), _pads_on(chip, s.sw_net))
        estimate: float | None = None
        if pair is not None:
            length, _ = c.path(*pair)
            estimate = octi_dist((pair[0].x, pair[0].y), (pair[1].x, pair[1].y))
            if length > REVIEW["M9_inductor_mm"]:
                what = f"inductor {_pin(pair[0])}->{_pin(pair[1])}: {length:.2f} mm"
                off.append(
                    Offender(
                        what, (s.inductor, s.ic), (s.sw_net,), (pair[0].x, pair[0].y), _r(length)
                    )
                )
        copper = c.track_len.get(s.sw_net) if c.stage == "routed" else None
        sw = copper if copper else estimate
        if sw is not None and sw > REVIEW["M9_sw_copper_mm"]:
            off.append(
                Offender(
                    f"sw_copper {s.sw_net}: {sw:.2f} mm",
                    (s.ic, s.inductor),
                    (s.sw_net,),
                    None,
                    _r(sw),
                )
            )
    return _metric("M9", len(off), _verdict(False, bool(off)), off)


# ---- M10 grouping --------------------------------------------------------------------------


def _m10(c: _Ctx) -> Metric:
    blocks = [(n, refs) for n, refs in c.m.groups if n.startswith("blk:")]
    if not blocks:
        blocks = [(b.name, b.refs) for b in c.cls.blocks]
    off: list[Offender] = []
    worst: float | None = None
    for name, refs in blocks:
        members = [c.parts[r] for r in refs if r in c.parts and len(c.parts[r].courtyard) >= 3]
        if len(members) < 2:
            continue
        hull = convex_hull([pt for p in members for pt in p.courtyard])
        total = sum(polygon_area(p.courtyard) for p in members)
        if len(hull) < 3 or total <= 0:
            continue
        ratio = polygon_area(hull) / total
        worst = ratio if worst is None else max(worst, ratio)
        names = tuple(sorted((p.ref for p in members), key=natural_key))
        if ratio > REVIEW["M10_ratio"]:
            off.append(
                Offender(f"spread {name}: hull/courtyards {ratio:.2f}", names, (), None, _r(ratio))
            )
        tops = sum(1 for p in members if p.side == "top")
        side = "top" if tops * 2 >= len(members) else "bottom"
        hb = _bbox(hull)
        inside = set(refs)
        for p in c.m.parts:
            if p.ref in inside or p.side != side or len(p.courtyard) < 3:
                continue
            if (
                _bbox_hit(_bbox(p.courtyard), hb)
                and c.m.placed(p)
                and polygons_overlap(p.courtyard, hull)
            ):
                off.append(Offender(f"intrusion {name}: {p.ref}", (p.ref,), (), (p.x, p.y), None))
    if worst is None:
        return _metric("M10", None, "n/a")
    return _metric("M10", _r(worst), _verdict(False, bool(off)), off)


# ---- M11 planes ----------------------------------------------------------------------------


def _disc_covers(a: Point, b: Point, discs: Sequence[tuple[Point, float]]) -> bool:
    """Segment ab lies within the union of the discs (centre, radius)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    aa = dx * dx + dy * dy
    spans: list[tuple[float, float]] = []
    for (cx, cy), r in discs:
        fx, fy = a[0] - cx, a[1] - cy
        if aa <= _EPS:
            if fx * fx + fy * fy <= r * r:
                return True
            continue
        bb = 2 * (fx * dx + fy * dy)
        disc = bb * bb - 4 * aa * (fx * fx + fy * fy - r * r)
        if disc < 0:
            continue
        sq = math.sqrt(disc)
        spans.append(((-bb - sq) / (2 * aa), (-bb + sq) / (2 * aa)))
    reach = 0.0
    for t0, t1 in sorted(spans):
        if t0 > reach + 1e-9:
            return False
        reach = max(reach, t1)
        if reach >= 1.0 - 1e-9:
            return True
    return False


class _PlaneVoids:
    """The voids of one filled plane zone on one layer: the region inside the outline outside
    every filled polygon ("outer") and every hole that is not antipads / thermal gaps (its
    boundary lies within the union of the discs of the vias / plated pads near it)."""

    def __init__(
        self,
        filled: Sequence[tuple[int, Sequence[Point], Sequence[Sequence[Point]]]],
        items: Sequence[tuple[Point, float]],
        outline: _Ring,
    ) -> None:
        self.outline = outline
        igrid = _Grid()
        for i, ((x, y), reach) in enumerate(items):
            igrid.add(i, (x - reach, y - reach, x + reach, y + reach))
        self.polys: list[tuple[_Ring, list[tuple[str, _Ring]]]] = []
        self.edges: list[tuple[Point, Point, str]] = []
        for i, outer, holes in filled:
            if len(outer) < 3:
                continue
            voids: list[tuple[str, _Ring]] = []
            for j, hole in enumerate(holes):
                if len(hole) < 3:
                    continue
                ring = _Ring(hole)
                discs = [items[k] for k in igrid.query(ring.bbox)]
                if discs and all(_disc_covers(a, b, discs) for a, b in _ring_edges(hole)):
                    continue  # via / plated pad antipads (merged) or thermal-relief gaps
                voids.append((f"hole {i}.{j}", ring))
                self.edges += [(a, b, f"hole {i}.{j}") for a, b in _ring_edges(hole)]
            self.polys.append((_Ring(outer), voids))
            self.edges += [(a, b, "outer") for a, b in _ring_edges(outer)]
        self.grid = _Grid()
        for k, (a, b, _) in enumerate(self.edges):
            self.grid.add(k, _bbox((a, b)))

    def void_at(self, p: Point) -> str | None:
        """The void id containing `p`, None when `p` is covered or outside the outline."""
        if not self.outline.contains(p):
            return None
        hole: str | None = None
        for outer, voids in self.polys:
            if not outer.contains(p):
                continue
            hit = next((vid for vid, ring in voids if ring.contains(p)), None)
            if hit is None:
                return None
            hole = hole or hit
        return hole or "outer"

    def crossed(self, a: Point, b: Point) -> set[str]:
        out = {
            self.edges[k][2]
            for k in self.grid.query(_bbox((a, b)))
            if _seg_hit(a, b, self.edges[k][0], self.edges[k][1])
        }
        for p in (a, b):
            vid = self.void_at(p)
            if vid is not None:
                out.add(vid)
        return out


def _m11(c: _Ctx) -> Metric:
    inner = c.m.copper_layers[1:-1]
    planes = [
        (z, layer)
        for z in c.m.zones
        if not z.rule_area and z.net and c.kind(z.net) == "ground"
        for layer in z.layers
        if layer in inner
    ]
    if not planes or c.outline is None:
        return _metric("M11", None, "n/a")
    board_area = polygon_area(c.outline)
    outline = _Ring(c.outline)
    # (centre, reach): a hole whose boundary lies within the reach discs of the vias /
    # plated pads near it is their (merged) antipad or thermal-relief gap, not a void
    items = [(v.at, v.diameter / 2 + ANTIPAD_MARGIN_MM) for v in c.m.vias] + [
        ((q.x, q.y), math.hypot(q.w, q.h) / 2 + ANTIPAD_MARGIN_MM)
        for p in c.m.parts
        for q in p.pads
        if q.drill is not None and _copper(q)
    ]
    off: list[Offender] = []
    worst: float | None = None
    layers = list(c.m.copper_layers)
    for z, layer in planes:
        holes = list(z.filled_holes) + [()] * (len(z.filled) - len(z.filled_holes))
        per_layer = len(z.filled_layers) == len(z.filled) > 0
        filled = [
            (i, poly, holes[i])
            for i, poly in enumerate(z.filled)
            if not per_layer or z.filled_layers[i] == layer
        ]
        if not filled and (per_layer or z.filled_area_mm2 <= 0):
            off.append(Offender(f"zone not filled: {z.name} on {layer}", (), (z.net,), None, None))
            continue
        if filled:
            area = sum(polygon_area(p) - sum(polygon_area(h) for h in hs) for _, p, hs in filled)
        else:
            area = z.filled_area_mm2
        # without per-layer polygons the zone's total is shared evenly by its copper layers
        n_layers = 1 if per_layer else max(1, sum(1 for la in z.layers if la in layers))
        ratio = area / n_layers / board_area if board_area > 0 else 0.0
        worst = ratio if worst is None else min(worst, ratio)
        if ratio < REVIEW["M11_fill"]:
            off.append(
                Offender(f"fill {z.name} on {layer}: {ratio:.3f}", (), (z.net,), None, _r(ratio))
            )
        if not filled:
            continue
        voids = _PlaneVoids(filled, items, outline)
        i = layers.index(layer)
        adjacent = {layers[k] for k in (i - 1, i + 1) if 0 <= k < len(layers)}
        segs: list[tuple[str, Point, Point]] = [
            (t.net, t.start, t.end) for t in c.m.tracks if t.layer in adjacent and c.signal(t.net)
        ]
        for a in c.m.arcs:
            if a.layer in adjacent and c.signal(a.net):
                segs += [(a.net, a.start, a.mid), (a.net, a.mid, a.end)]
        seen: set[tuple[str, str]] = set()
        for net, p, q in segs:
            for vid in sorted(voids.crossed(p, q)):
                if (net, vid) in seen:
                    continue
                seen.add((net, vid))
                mid = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
                what = f"void crossing: {net} over {z.name} {layer} {vid}"
                off.append(Offender(what, (), (net,), mid, None))
    value = None if worst is None else _r(worst)
    return _metric("M11", value, _verdict(False, bool(off)), off)


# ---- M12 widths and pairs ------------------------------------------------------------------


def _m12(c: _Ctx) -> Metric:
    classes = {nc.name: nc for nc in c.m.net_classes}
    class_of = dict(c.m.net_class_of)
    off: list[Offender] = []
    items = [(t.net, t.width, t.start, t.end) for t in c.m.tracks]
    items += [(a.net, a.width, a.start, a.mid) for a in c.m.arcs]
    for net, width, p, q in items:
        nc = classes.get(class_of.get(net, "Default"))
        if nc is None or width >= nc.track_width - M12_WIDTH_TOL - 1e-9:
            continue
        mid = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
        what = f"track_width {net}: {width:g} < {nc.track_width:g} ({nc.name})"
        off.append(Offender(what, (), (net,), mid, _r(nc.track_width - width)))
    widths = len(off)
    review = False
    for pair in c.cls.pairs:
        lp, ln = c.track_len.get(pair.p, 0.0), c.track_len.get(pair.n, 0.0)
        if lp > 0 and ln > 0 and abs(lp - ln) > REVIEW["M12_mismatch_mm"]:
            review = True
            mm = abs(lp - ln)
            what = f"pair_mismatch {pair.p}/{pair.n}: {mm:.2f} mm"
            off.append(Offender(what, (), (pair.p, pair.n), None, _r(mm)))
    return _metric("M12", widths, _verdict(widths > 0, review), off)


# ---- M13 edges and keepouts ----------------------------------------------------------------


def _edge_gap(cy: Sequence[Point], box: BBox, edge: Edge) -> float:
    x0, y0, x1, y1 = _bbox(cy)
    return {
        "left": x0 - box[0],
        "right": box[2] - x1,
        "top": y0 - box[1],
        "bottom": box[3] - y1,
    }[edge]


def _seg_in_poly(a: Point, b: Point, ring: _Ring) -> bool:
    if ring.contains(a) or ring.contains(b):
        return True
    return any(_seg_hit(a, b, p, q) for p, q in _ring_edges(ring.pts))


def _m13(c: _Ctx) -> Metric:
    off: list[Offender] = []
    box = _bbox(c.outline) if c.outline is not None else None
    for row in c.doc.edges:
        if row.edge is None:
            continue
        p = c.parts.get(row.ref)
        if p is None or len(p.courtyard) < 3 or box is None:
            why = "not on the board" if p is None else "no courtyard or outline"
            off.append(Offender(f"edge {row.ref}: {why}", (row.ref,), (), None, None))
            continue
        gap = _edge_gap(p.courtyard, box, row.edge)
        ok = gap <= M13_EDGE_MM + _EPS and (gap >= -_EPS or placed_with_overhang(c.m, p, row.edge))
        if not ok:
            what = f"edge {row.ref}: {gap:.2f} mm from the {row.edge} edge"
            off.append(Offender(what, (row.ref,), (), (p.x, p.y), _r(abs(gap))))
    # keepout areas: (owner ref, ring, layers or None = all, tracks?, vias?)
    areas: list[tuple[str, _Ring, frozenset[str] | None, bool, bool]] = []
    modules = [p for p in c.m.parts if c.cls.part_class.get(p.ref) == "module_with_antenna"]
    for p in modules:
        areas += [(p.ref, _Ring(ra), None, True, True) for ra in p.rule_areas if len(ra) >= 3]
        if len(p.courtyard) < 3:
            continue
        for z in c.m.zones:
            if not (z.rule_area and z.keepout and len(z.outline) >= 3):
                continue
            if not polygon_inside(z.outline, p.courtyard):
                continue
            layers = None if not z.layers or "*.Cu" in z.layers else frozenset(z.layers)
            tracks = bool(z.keepout & {"tracks", "copper"})
            vias = bool(z.keepout & {"vias", "copper"})
            areas.append((p.ref, _Ring(z.outline), layers, tracks, vias))
    for ref, ring, layers, tracks, vias in areas:
        if tracks:
            segs = [(t.net, t.layer, t.start, t.end) for t in c.m.tracks]
            segs += [(a.net, a.layer, a.start, a.mid) for a in c.m.arcs]
            segs += [(a.net, a.layer, a.mid, a.end) for a in c.m.arcs]
            for net, layer, s, e in segs:
                if layers is not None and layer not in layers:
                    continue
                if _bbox_hit(_bbox((s, e)), ring.bbox) and _seg_in_poly(s, e, ring):
                    off.append(Offender(f"keepout {ref}: track {net}", (ref,), (net,), s, None))
        if vias:
            for v in c.m.vias:
                if ring.contains(v.at):
                    off.append(
                        Offender(f"keepout {ref}: via {v.net}", (ref,), (v.net,), v.at, None)
                    )
    return _metric("M13", len(off), _verdict(bool(off)), off)


# ---- M14 fab rules -------------------------------------------------------------------------

_BOARD_RULES: Mapping[str, str] = MappingProxyType(
    {
        "min_track_width": "min_track",
        "min_clearance": "min_space",
        "min_via_annular_width": "min_annular",
        "min_via_size": "min_via_diameter",
        "min_through_drill": "min_drill",
        "copper_edge_clearance": "copper_to_edge",
        "hole_to_hole_min": "hole_to_hole",
        "min_silk_text_height": "silk_min_height",
        "min_silk_text_thickness": "silk_min_line",
    }
)
_CONDITION = re.compile(r'\(\s*condition\s+"((?:[^"\\]|\\.)*)"', re.IGNORECASE)


def _text_poly(t: TextItem) -> list[Point]:
    lines = t.text.split("\n") or [""]
    w = max(len(line) for line in lines) * t.size[0] * SILK_CHAR_WIDTH
    h = len(lines) * t.size[1]
    if t.kind == "textbox":
        return rect_poly(t.at[0], t.at[1], w, h)
    return rect_poly(t.at[0] - w / 2, t.at[1] - h / 2, w, h)


def _graphic_segments(kind: str, pts: Sequence[Point]) -> list[tuple[Point, Point]]:
    """Straight pieces of a silk graphic (arcs, circles and beziers as fine polylines)."""
    if kind == "segment" and len(pts) == 2:
        return [(pts[0], pts[1])]
    if kind == "rect" and len(pts) == 2:
        (x0, y0), (x1, y1) = pts
        return _ring_edges([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    if kind == "polygon" and len(pts) >= 2:
        return _ring_edges(pts)
    if kind == "circle" and len(pts) == 2:
        r = math.dist(pts[0], pts[1])
        ring = [
            (pts[0][0] + r * math.cos(a), pts[0][1] + r * math.sin(a))
            for a in (2 * math.pi * k / 64 for k in range(64))
        ]
        return _ring_edges(ring)
    if kind == "arc" and len(pts) == 3:
        return list(itertools.pairwise(_arc_points(pts[0], pts[1], pts[2])))
    if kind == "bezier" and len(pts) == 4:
        p0, p1, p2, p3 = pts
        curve = [
            (
                (1 - t) ** 3 * p0[0]
                + 3 * (1 - t) ** 2 * t * p1[0]
                + 3 * (1 - t) * t * t * p2[0]
                + t**3 * p3[0],
                (1 - t) ** 3 * p0[1]
                + 3 * (1 - t) ** 2 * t * p1[1]
                + 3 * (1 - t) * t * t * p2[1]
                + t**3 * p3[1],
            )
            for t in (k / 32 for k in range(33))
        ]
        return list(itertools.pairwise(curve))
    return list(itertools.pairwise(pts))


def _arc_points(a: Point, m: Point, b: Point, n: int = 32) -> list[Point]:
    """`n` chords along the arc from a through m to b."""
    circle = _circle(a, m, b)
    if circle is None:
        return [a, m, b]
    ux, uy, r = circle
    t0 = math.atan2(a[1] - uy, a[0] - ux)
    tm = math.atan2(m[1] - uy, m[0] - ux)
    t1 = math.atan2(b[1] - uy, b[0] - ux)
    span = (t1 - t0) % (2 * math.pi)
    if (tm - t0) % (2 * math.pi) > span:  # the mid point lies the other way round
        span -= 2 * math.pi
    return [
        (ux + r * math.cos(t0 + span * k / n), uy + r * math.sin(t0 + span * k / n))
        for k in range(n + 1)
    ]


def _seg_hits_convex(a: Point, b: Point, poly: Sequence[Point], inflate: float) -> bool:
    """Segment ab inflated by `inflate` overlaps the convex polygon `poly` (touching is not
    overlap)."""
    ring = _Ring(poly)
    if ring.contains(a) or ring.contains(b):
        return True
    return min(_seg_seg(a, b, p, q) for p, q in _ring_edges(poly)) < inflate - _EPS


class _Edges:
    """The outline's edges in a bucket grid (distance queries for M14)."""

    def __init__(self, outline: Sequence[Point]) -> None:
        self.ring = _Ring(outline)
        self.edges = _ring_edges(outline)
        self.grid = _Grid()
        for k, (a, b) in enumerate(self.edges):
            self.grid.add(k, _bbox((a, b)))

    def clearance(self, pieces: Sequence[tuple[Point, Point]], reach: float) -> float:
        """Distance from the pieces to the outline (negative when the first point is outside;
        `reach` when no edge is within `reach`)."""
        if not self.ring.contains(pieces[0][0]):
            return -1.0
        box = _bbox([p for seg in pieces for p in seg], reach)
        best = reach
        for k in self.grid.query(box):
            c, d = self.edges[k]
            for a, b in pieces:
                best = min(best, _seg_seg(a, b, c, d))
        return best


class _Fab:
    """M14 checks against one preset; offenders collected in `raw`."""

    def __init__(self, c: _Ctx, pr: FabPreset) -> None:
        self.c, self.pr = c, pr
        self.raw: list[Offender] = []
        self.pads = [(p, q) for p in c.m.parts for q in p.pads]

    def add(
        self,
        kind: str,
        detail: str,
        at: Point | None,
        value: float | None = None,
        refs: Iterable[str] = (),
        nets: Iterable[str] = (),
    ) -> None:
        refs_t = tuple(sorted({r for r in refs if r}, key=natural_key))
        nets_t = tuple(sorted({n for n in nets if n}))
        v = None if value is None else _r(value)
        self.raw.append(Offender(f"{kind}: {detail}", refs_t, nets_t, at, v))

    def below(
        self,
        kind: str,
        who: str,
        value: float,
        limit: float,
        at: Point | None,
        refs: Iterable[str] = (),
        nets: Iterable[str] = (),
    ) -> None:
        if value < limit - _EPS:
            self.add(kind, f"{who} {value:g} < {limit:g}", at, limit - value, refs, nets)

    def rules(self) -> None:
        for name, v in self.c.m.rules:
            attr = _BOARD_RULES.get(name)
            if attr is None:
                continue
            limit = float(getattr(self.pr, attr))
            if v < limit - _RULE_TOL:
                self.add("board_rule", f"{name} {v:g} < {limit:g} ({self.pr.id})", None, limit - v)

    def items(self) -> None:
        pr = self.pr
        for t in self.c.m.tracks:
            self.below("track_width", t.net, t.width, pr.min_track, t.start, nets=(t.net,))
        for arc in self.c.m.arcs:
            self.below("track_width", arc.net, arc.width, pr.min_track, arc.mid, nets=(arc.net,))
        for v in self.c.m.vias:
            n = (v.net,)
            self.below("via_drill", v.net, v.drill, pr.min_via_drill, v.at, nets=n)
            self.below("via_diameter", v.net, v.diameter, pr.min_via_diameter, v.at, nets=n)
            ring = (v.diameter - v.drill) / 2
            self.below("annular_ring", v.net, ring, pr.min_annular, v.at, nets=n)
        for p, q in self.pads:
            if q.drill is None:
                continue
            kind, limit = ("pth_drill", pr.min_drill) if _copper(q) else ("npth_drill", pr.min_npth)
            self.below(kind, _pin(q), q.drill, limit, (q.x, q.y), (p.ref,), (q.net,))

    def edges(self) -> None:
        if self.c.outline is None:
            return
        edges = _Edges(self.c.outline)
        c2e = self.pr.copper_to_edge
        reach = c2e + 1.0

        def check(
            kind: str,
            who: str,
            pieces: list[tuple[Point, Point]],
            half: float,
            refs: tuple[str, ...] = (),
            nets: tuple[str, ...] = (),
        ) -> None:
            gap = edges.clearance(pieces, reach + half) - half
            if gap < c2e - _EPS:
                detail = f"{who} {max(gap, 0.0):.3f} mm < {c2e:g}"
                self.add(kind, detail, pieces[0][0], c2e - gap, refs, nets)

        for t in self.c.m.tracks:
            check("copper_to_edge", f"track {t.net}", [(t.start, t.end)], t.width / 2, (), (t.net,))
        for arc in self.c.m.arcs:
            pts = list(itertools.pairwise(_arc_points(arc.start, arc.mid, arc.end, 8)))
            check("copper_to_edge", f"arc {arc.net}", pts, arc.width / 2, (), (arc.net,))
        for v in self.c.m.vias:
            check("copper_to_edge", f"via {v.net}", [(v.at, v.at)], v.diameter / 2, (), (v.net,))
            check("hole_to_edge", f"via {v.net}", [(v.at, v.at)], v.drill / 2, (), (v.net,))
        for p, q in self.pads:
            centre = (q.x, q.y)
            if _copper(q):
                poly = _pad_poly(q)
                pieces = [(centre, centre), *_ring_edges(poly)]
                check("copper_to_edge", f"pad {_pin(q)}", pieces, 0.0, (p.ref,), (q.net,))
            if q.drill is not None:
                pieces = [(centre, centre)]
                check("hole_to_edge", f"pad {_pin(q)}", pieces, q.drill / 2, (p.ref,), (q.net,))

    def silk(self) -> None:
        pr = self.pr
        texts = [(p.ref, t) for p in self.c.m.parts for t in p.texts]
        texts += [("", t) for t in self.c.m.texts]
        texts = [(r, t) for r, t in texts if t.visible and t.layer in _SILK and t.text.strip()]
        for ref, t in texts:
            who = f"{ref} {t.kind}" if ref else t.kind
            self.below("silk_text_height", who, t.size[1], pr.silk_min_height, t.at, (ref,))
            self.below("silk_line_width", who, t.thickness, pr.silk_min_line, t.at, (ref,))
        # mask openings bucketed per side (cell BUCKET_MM, keyed by the opening's bbox)
        mask: dict[str, tuple[list[tuple[Pad, list[Point]]], _Grid]] = {}
        for silk_layer, mask_layer in _SILK.items():
            openings = [(q, _pad_poly(q)) for _, q in self.pads if mask_layer in q.layers]
            grid = _Grid()
            for k, (_, poly) in enumerate(openings):
                grid.add(k, _bbox(poly))
            mask[silk_layer] = (openings, grid)
        for ref, t in texts:
            openings, grid = mask[t.layer]
            box = _text_poly(t)
            for k in grid.query(_bbox(box)):
                q, opening = openings[k]
                if polygons_overlap(box, opening):
                    detail = f"{ref or 'board'} {t.kind} over {_pin(q)}"
                    self.add("silk_over_pad", detail, t.at, None, (ref, q.ref), (q.net,))
        for g in self.c.m.graphics:
            if g.layer not in mask:
                continue
            openings, grid = mask[g.layer]
            hits: set[int] = set()
            for a, b in _graphic_segments(g.kind, g.points):
                for k in grid.query(_bbox((a, b), g.width / 2)):
                    if k not in hits and _seg_hits_convex(a, b, openings[k][1], g.width / 2):
                        hits.add(k)
            for k in sorted(hits):
                q = openings[k][0]
                detail = f"{g.layer} {g.kind} over {_pin(q)}"
                self.add("silk_over_pad", detail, (q.x, q.y), None, (q.ref,), (q.net,))

    def paste(self) -> None:
        for p, q in self.pads:
            if not q.smd or self.c.cls.part_class.get(p.ref) in ("fiducial", "test_point"):
                continue
            for cu, paste in (("F.Cu", "F.Paste"), ("B.Cu", "B.Paste")):
                if cu in q.layers and paste not in q.layers:
                    detail = f"{_pin(q)} has no {paste}"
                    self.add("paste_missing", detail, (q.x, q.y), None, (p.ref,), (q.net,))


def _excepted(o: Offender, conditions: Sequence[str]) -> bool:
    """Every ref of `o` is named in some custom rule condition of the `.kicad_dru`."""
    return bool(o.refs) and all(
        any(re.search(rf"(?<![\w.]){re.escape(r)}(?!\w)", cond) for cond in conditions)
        for r in o.refs
    )


def _m14(c: _Ctx) -> Metric:
    if c.preset is None:
        return _metric("M14", None, "n/a")
    fab = _Fab(c, c.preset)
    fab.rules()
    fab.items()
    fab.edges()
    fab.silk()
    fab.paste()
    conditions = _CONDITION.findall(c.m.dru_text)
    off: list[Offender] = []
    count = exceptions = 0
    for o in fab.raw:
        if _excepted(o, conditions):
            exceptions += 1
            off.append(replace(o, what=f"exception {o.what}"))
        else:
            count += 1
            off.append(o)
    return _metric("M14", count, _verdict(count > 0, exceptions > 0), off)


# ---- public --------------------------------------------------------------------------------


def compute_metrics(
    model: BoardModel,
    cls: Classification,
    doc: BoardDoc,
    *,
    drc: DrcResult | None,
    parity: tuple[DrcMarker, ...] | None,
    preset: FabPreset | None,
    only: frozenset[str] | None = None,
) -> MetricsReport:
    """Metrics M1-M14 (spec §12) of `model`; `only` restricts the computed metrics (the
    others are omitted from the report)."""
    c = _Ctx(model, cls, doc, preset)
    table: dict[MetricId, Callable[[], Metric]] = {
        "M1": lambda: _m1(c),
        "M2": lambda: _m2(c, drc),
        "M3": lambda: _m3(c, drc, parity),
        "M4": lambda: _m4(c),
        "M5": lambda: _m5(c),
        "M6": lambda: _m6(c),
        "M7": lambda: _m7(c),
        "M8": lambda: _m8(c),
        "M9": lambda: _m9(c),
        "M10": lambda: _m10(c),
        "M11": lambda: _m11(c),
        "M12": lambda: _m12(c),
        "M13": lambda: _m13(c),
        "M14": lambda: _m14(c),
    }
    out = tuple(table[mid]() for mid in METRIC_IDS if only is None or mid in only)
    return MetricsReport(
        metrics=out,
        hard_failures=sum(1 for x in out if x.verdict == "fail"),
        reviews=sum(1 for x in out if x.verdict == "review"),
        stage=c.stage,
    )
