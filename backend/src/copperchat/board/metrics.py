"""Quality metrics M1-M16 (B-C7, spec §12 and §22.5). Pure: no kipy import (`DrcMarker` /
`DrcResult` are only type-checked; they are read by attribute).

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
* M11 is per plane LAYER (spec §12): fill ratio = the filled area of every ground zone on
  the layer / board area, and one void structure from all their polygons on that layer (a
  seam between two touching fills is no void). A hole of a filled polygon whose boundary
  lies within the union of the discs (centre, via radius / pad half-diagonal +
  `ANTIPAD_MARGIN_MM`) of the vias / plated pads near it, and whose area is at most those
  discs' total area, is their antipad (also merged antipads of close vias) or
  thermal-relief gap, not a void; other holes (e.g. a removed island ringed by vias) and
  the region between the outline and the fills are voids. Each layer uses only its own
  filled polygons (`Zone.filled_layers`); without per-layer data (old models) all of a
  zone's polygons are used and its fill area is divided by its layer count. Adjacent
  layers = the copper layers next to the plane layer in `model.copper_layers` order.
* M15/M16 (round 2, §22.5) ship as review behind `M15_HARD` / `M16_HARD` (R4 switches them):
  their hard offenders get verdict `review` while a switch is off. M16's corridor width is
  the narrower neck width of the two pads (`geom.neck_width` of the net-class width), its
  clearance the net class's; footprint rule areas count as keepouts on every layer (as the
  router treats them). Arcs on a routed path appear as two chords (start-mid, mid-end), so
  an arc between adjacent pads is never "one straight segment".
* M14 silk text bbox: axis-aligned (90°/270° texts swap width and height), `len(line) x size_x x
  SILK_CHAR_WIDTH` wide and `lines x size_y` high, centred on `at` (top-left for text
  boxes). Fiducials and test points are exempt from the paste rule (they are never pasted;
  KiCad's TestPoint_Pad_* have no paste by design).
"""

from __future__ import annotations

import functools
import heapq
import itertools
import math
import re
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, cast, get_args

from copperchat.board.boardmd import BoardDoc, Edge
from copperchat.board.classify import Classification
from copperchat.board.geom import (
    _JOIN_TOL,
    ADJ_GAP_MM,
    BendReport,
    TrackIndex,
    _Grid,
    _in_pad,
    _interior_t,
    _Nodes,
    _pad_on_layer,
    _proper_cross,
    analyse_entry,
    analyse_tracks,
    convex_hull,
    entry_zone,
    escape_zone,
    neck_link_zones,
    neck_width,
    octi_dist,
    polygon_area,
    polygon_inside,
    polygons_overlap,
    rect_poly,
    straight_corridor,
    straight_link_corridor,
)
from copperchat.board.model import (
    ArcTrack,
    BoardModel,
    NetClassInfo,
    Pad,
    Part,
    Point,
    TextItem,
    Track,
    Via,
)
from copperchat.board.presets import PRESETS, FabPreset
from copperchat.kicad_tools.filtering import natural_key

if TYPE_CHECKING:
    from copperchat.kicad_tools.board.api import DrcMarker, DrcResult

__all__ = [
    "HARD",
    "M1_EDGE_OVERHANG",
    "M15_HARD",
    "M16_HARD",
    "MAX_OFFENDERS",
    "REVIEW",
    "M16Defect",
    "Metric",
    "MetricId",
    "MetricsReport",
    "Offender",
    "Verdict",
    "compute_metrics",
    "connections",
    "m16_defects",
    "neck_checker",
    "outline_polygon",
    "placed_with_overhang",
    "routed_length",
    "routed_path",
]

# fmt: off
MetricId = Literal[
    "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9", "M10", "M11", "M12", "M13", "M14",
    "M15", "M16",
]
# fmt: on
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
        "M16_align_mm": 1.0,
    }
)
M1_EDGE_OVERHANG = True  # owner decision 2026-11-25 (index "Provisional" 2)
# round 2 (§22.5): Y-junctions (M15) and M16 doglegs / pad loops / pad jogs (owner
# 2026-11-27: jogs too) are hard once R4's router and post-processor produce them clean
M15_HARD = True
M16_HARD = True

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
        "M3": (
            "errors + unaccepted warnings + parity > 0",
            "accepted warnings and footprint silk-line warnings listed",
        ),
        "M4": (f"> 0 (off 0/45/90 by > {_g(HARD['M4_angle_tol_deg'])}°, 90°, acute)", None),
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
        "M15": ("Y-junctions > 0 (branch off one 90° lattice)", "Y-junctions listed"),
        "M16": (
            "doglegs between adjacent pads + pad-entry loops + pad-entry jogs > 0",
            f"pairs alignable by <= {_g(REVIEW['M16_align_mm'])} mm",
        ),
    }
)


# ---- small geometry helpers ----------------------------------------------------------------

BBox = tuple[float, float, float, float]  # x0, y0, x1, y1


def _bbox(pts: Iterable[Point], grow: float = 0.0) -> BBox:
    xs, ys = zip(*pts, strict=True)
    return (min(xs) - grow, min(ys) - grow, max(xs) + grow, max(ys) + grow)


def _grow(b: BBox, d: float) -> BBox:
    return (b[0] - d, b[1] - d, b[2] + d, b[3] + d)


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


def _placer(model: BoardModel) -> Callable[[Part], bool]:
    """`BoardModel.placed` with the outline polygon built once (a rounded rect would be
    rebuilt per call there); identical results."""
    o = model.outline
    if o.kind == "none":
        return lambda part: False
    if o.kind == "rect" and o.corner_radius > 0:
        rounded = rect_poly(o.x, o.y, o.w, o.h, o.corner_radius)
        # the rounded rect is convex and contains the two "cross" rectangles; a courtyard
        # strictly inside one of them (1 um margin, far above the 1e-9 tolerances) is inside
        # by `polygon_inside` too, so only the rest pays for the full test
        r = min(o.corner_radius, o.w / 2, o.h / 2)
        m = 1e-6
        cross = (
            (o.x + r + m, o.y + m, o.x + o.w - r - m, o.y + o.h - m),
            (o.x + m, o.y + r + m, o.x + o.w - m, o.y + o.h - r - m),
        )

        def rounded_placed(part: Part) -> bool:
            if len(part.courtyard) < 3:
                return False
            x0, y0, x1, y1 = _bbox(part.courtyard)
            if any(a < x0 and b < y0 and x1 < c and y1 < d for a, b, c, d in cross):
                return True
            return polygon_inside(part.courtyard, rounded)

        return rounded_placed
    if o.kind == "rect":
        x0, y0, x1, y1 = o.x, o.y, o.x + o.w, o.y + o.h
        return lambda part: (
            len(part.courtyard) >= 3
            and all(x0 <= px <= x1 and y0 <= py <= y1 for px, py in part.courtyard)
        )
    poly = o.polygon
    return lambda part: (
        len(part.courtyard) >= 3 and len(poly) >= 3 and polygon_inside(part.courtyard, poly)
    )


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
    where another end, a via centre or a pad centre (pad on that layer) lies on their
    interior (T), vias join every layer at 0 cost, a pad joins every track end inside it and
    every via whose centre lies in it (via-in-pad) at 0 cost."""

    def __init__(
        self,
        tracks: Sequence[Track],
        arcs: Sequence[ArcTrack],
        vias: Sequence[Via],
        pads: Sequence[Pad],
    ) -> None:
        """`tracks`, `arcs`, `vias` and `pads` are the net's own."""
        self.adj: dict[_Node, list[tuple[_Node, float]]] = defaultdict(list)
        # round 2: track width and (arcs) mid point of every copper edge, for `routed_path`
        self.width: dict[frozenset[_Node], float] = {}
        self.arc_mid: dict[frozenset[_Node], Point] = {}
        # `_search` results per (pad, pad): M5/M6 (`conns`) and M16 ask for the same pairs
        self._memo: dict[tuple[_Node, _Node], tuple[float, list[_Node]] | None] = {}
        by_layer: dict[str, list[tuple[Point, Point, float, bool, float, Point | None]]]
        by_layer = defaultdict(list)
        nodes: dict[str, _Nodes] = defaultdict(_Nodes)
        for t in tracks:
            if math.dist(t.start, t.end) >= _EPS:
                a, b = nodes[t.layer].snap(t.start), nodes[t.layer].snap(t.end)
                if a != b:
                    by_layer[t.layer].append((a, b, math.dist(a, b), True, t.width, None))
        for arc in arcs:
            a, b = nodes[arc.layer].snap(arc.start), nodes[arc.layer].snap(arc.end)
            if a != b:
                length = _arc_len(a, arc.mid, b)
                by_layer[arc.layer].append((a, b, length, False, arc.width, arc.mid))
        via_at = [v.at for v in vias]
        ends: dict[str, set[Point]] = {}
        for layer, segs in by_layer.items():
            # track ends and via centres split a straight track they lie on (a T)
            pts = {p for a, b, *_ in segs for p in (a, b)}
            pts |= {nodes[layer].snap(v) for v in via_at}
            pts |= {nodes[layer].snap((q.x, q.y)) for q in pads if _pad_on_layer(q, layer)}
            ends[layer] = pts
            grid = _Grid()
            plist = sorted(pts)
            for i, p in enumerate(plist):
                grid.add(i, (p[0], p[1], p[0], p[1]))
            for a, b, length, straight, width, mid in segs:
                if straight:
                    cuts = []
                    for i in grid.query(_bbox((a, b), _JOIN_TOL)):
                        tp = _interior_t(plist[i], a, b)
                        if tp is not None:
                            cuts.append((tp, plist[i]))
                    chain = [a, *(p for _, p in sorted(cuts)), b]
                    for p, q in itertools.pairwise(chain):
                        self._edge(("t", layer, p), ("t", layer, q), math.dist(p, q), width)
                else:
                    self._edge(("t", layer, a), ("t", layer, b), length, width)
                    if mid is not None:
                        self.arc_mid[frozenset((("t", layer, a), ("t", layer, b)))] = mid
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
        pad_grid = _Grid()
        for j, pad in enumerate(pads):
            r = math.hypot(pad.w, pad.h) / 2 + _JOIN_TOL
            pad_grid.add(j, _bbox(((pad.x, pad.y),), r))
            for i in grid.query(_bbox(((pad.x, pad.y),), r)):
                layer, p = all_nodes[i]
                if _pad_on_layer(pad, layer) and _in_pad(p, pad):
                    self._edge(self.pad_node(pad), ("t", layer, p), 0.0)
        for k, v in enumerate(vias):  # via-in-pad: a via spans every layer of the pad
            for j in pad_grid.query(_bbox((v.at,))):
                if _copper(pads[j]) and _in_pad(v.at, pads[j]):
                    self._edge(("v", k), self.pad_node(pads[j]), 0.0)

    @staticmethod
    def pad_node(p: Pad) -> _Node:
        return ("p", p.ref, p.number, p.x, p.y)

    def _edge(self, a: _Node, b: _Node, w: float, width: float | None = None) -> None:
        self.adj[a].append((b, w))
        self.adj[b].append((a, w))
        if width is not None:
            key = frozenset((a, b))
            self.width[key] = max(width, self.width.get(key, 0.0))

    def _search(self, a: Pad, b: Pad) -> tuple[float, list[_Node]] | None:
        """Dijkstra from pad `a` to pad `b`: (length, node sequence a..b), None when not
        connected. `length` and `path` share it."""
        src, dst = self.pad_node(a), self.pad_node(b)
        key = (src, dst)
        if key not in self._memo:
            self._memo[key] = self._dijkstra(src, dst)
        return self._memo[key]

    def _dijkstra(self, src: _Node, dst: _Node) -> tuple[float, list[_Node]] | None:
        if src == dst:
            return 0.0, [src]
        if src not in self.adj or dst not in self.adj:
            return None
        dist: dict[_Node, float] = {src: 0.0}
        pred: dict[_Node, _Node] = {}
        heap: list[tuple[float, int, _Node]] = [(0.0, 0, src)]
        tie = itertools.count(1)
        while heap:
            d, _, n = heapq.heappop(heap)
            if n == dst:
                seq = [n]
                while seq[-1] != src:
                    seq.append(pred[seq[-1]])
                return d, seq[::-1]
            if d > dist.get(n, math.inf):
                continue
            for m, w in self.adj[n]:
                nd = d + w
                if nd < dist.get(m, math.inf) - 1e-12:
                    dist[m] = nd
                    pred[m] = n
                    heapq.heappush(heap, (nd, next(tie), m))
        return None

    def length(self, a: Pad, b: Pad) -> float | None:
        hit = self._search(a, b)
        return None if hit is None else hit[0]

    def path(self, a: Pad, b: Pad) -> list[_Node] | None:
        """The shortest path's nodes from pad `a` to pad `b` (pad, track and via nodes)."""
        hit = self._search(a, b)
        return None if hit is None else hit[1]

    def segments(self, nodes: Sequence[_Node]) -> tuple[tuple[str, Point, Point, float], ...]:
        """The copper edges along `nodes` (`path`) as (layer, start, end, width) in path
        order; zero-cost pad / via joins are skipped, an arc becomes its two chords."""
        out: list[tuple[str, Point, Point, float]] = []
        for u, v in itertools.pairwise(nodes):
            if u[0] != "t" or v[0] != "t" or u[1] != v[1]:
                continue
            key = frozenset((u, v))
            layer, p, q = str(u[1]), _as_point(u[2]), _as_point(v[2])
            w = self.width.get(key, 0.0)
            mid = self.arc_mid.get(key)
            if mid is None:
                out.append((layer, p, q, w))
            else:
                out += [(layer, p, mid, w), (layer, mid, q, w)]
        return tuple(out)


def _as_point(o: object) -> Point:
    """A track node's point (`_Node` is loosely typed)."""
    return cast(Point, o)


def _net_graph(model: BoardModel, net: str) -> _NetGraph:
    return _NetGraph(
        [t for t in model.tracks if t.net == net],
        [arc for arc in model.arcs if arc.net == net],
        [v for v in model.vias if v.net == net],
        _net_pads(model, net),
    )


def routed_length(model: BoardModel, a: Pad, b: Pad) -> float | None:
    """Shortest path between pads `a` and `b` through their net's track/via graph (vias
    0 mm); None when they are not connected by copper (or on different nets)."""
    if a.net != b.net or not a.net:
        return None
    return _net_graph(model, a.net).length(a, b)


def routed_path(model: BoardModel, a: Pad, b: Pad) -> tuple[tuple[str, Point, Point], ...] | None:
    """The copper segments (layer, start, end) of `routed_length`'s shortest path from pad
    `a` to pad `b`, in path order (via / pad joins skipped; an arc as its two chords); None
    when they are not connected (or on different nets). Builds the net's graph per call
    (like `routed_length`); `compute_metrics` reuses one graph per net. A straight track and
    an arc with the same two ends share one graph edge, and the edge is then reported as the
    arc's chords (a corner case no router produces)."""
    if a.net != b.net or not a.net:
        return None
    g = _net_graph(model, a.net)
    nodes = g.path(a, b)
    if nodes is None:
        return None
    return tuple((la, p, q) for la, p, q, _ in g.segments(nodes))


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
        # P7: also the footprints' pad / field / text / shape KIIDs (DRC markers name
        # those, e.g. silk_edge_clearance on a reference), so such warnings map to a ref
        self.ref_of_kiid = {k: p.ref for p in model.parts for k, _ in p.item_kiids}
        self.ref_of_kiid.update({p.kiid: p.ref for p in model.parts})
        self.pads_by_net: dict[str, list[Pad]] = defaultdict(list)
        self.parts_of_net: dict[str, list[Part]] = defaultdict(list)
        for p in model.parts:
            for q in p.pads:
                self.pads_by_net[q.net].append(q)
            for net in sorted({q.net for q in p.pads if q.net}):
                self.parts_of_net[net].append(p)
        self.role_of_kiid = {k: role for p in model.parts for k, role in p.item_kiids}
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
        self._zones: dict[str, tuple[_Grid, list[tuple[tuple[Point, ...], Part]]]] = {}
        self._bends: dict[str, BendReport] | None = None
        self._conns: list[_Conn] | None = None
        self._placer = _placer(model)
        self._placed: dict[int, bool] = {}  # id(part) -> placed (parts live as long as `m`)

    def placed(self, part: Part) -> bool:
        """`BoardModel.placed`, computed once per part (shared by M1 and M10)."""
        key = id(part)
        hit = self._placed.get(key)
        if hit is None:
            hit = self._placed[key] = self._placer(part)
        return hit

    def escape_zones(self, net: str, box: BBox) -> list[tuple[tuple[Point, ...], Part]]:
        """(escape zone, part) of the parts with a pad of `net` whose zone bbox meets `box`
        (M12 neck-downs; one bucket grid per net, built on first use)."""
        hit = self._zones.get(net)
        if hit is None:
            items: list[tuple[tuple[Point, ...], Part]] = []
            grid = _Grid()
            for part in self.parts_of_net.get(net, ()):
                zone = escape_zone(part)
                if zone:
                    grid.add(len(items), _bbox(zone))
                    items.append((zone, part))
            hit = self._zones[net] = (grid, items)
        grid, items = hit
        return [items[i] for i in grid.query(box)]

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
        """`analyse_tracks` per net, each on the board restricted to the net's tracks/arcs,
        with one pad/via index of the whole board shared by every net (the sum equals
        `analyse_tracks(model)`)."""
        if self._bends is not None:
            return self._bends
        tracks, arcs = self.tracks_by_net, self.arcs_by_net
        index = TrackIndex(self.m)
        out: dict[str, BendReport] = {}
        for net in sorted(set(tracks) | set(arcs)):
            sub = replace(self.m, tracks=tuple(tracks.get(net, ())), arcs=tuple(arcs.get(net, ())))
            out[net] = analyse_tracks(sub, index=index)
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
    if (mid == "M15" and not M15_HARD) or (mid == "M16" and not M16_HARD):
        # switched off (round 2 until R4): no hard threshold yet, the hard part is reviewed
        hard, review = None, f"{hard} (hard from round 2 R4); {review}"
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
        placed = c.placed(p)
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
    count = accepted = silk_lines = 0
    for mk in drc.markers if drc is not None else ():
        t = _norm_type(mk.type)
        if t.startswith("unconnected") or mk.severity not in ("error", "warning"):
            continue
        refs = c.refs(mk.kiids)
        if (
            t == "text_thickness"
            and mk.kiids
            and all(c.role_of_kiid.get(k) == "shape" for k in mk.kiids)
        ):
            # owner decision 1 (2026-11-26, spec §22.2): footprint silk LINES below the
            # silk minimum are a warning only; silk text keeps the hard rule
            silk_lines += 1
            what = f"silk_line_warning: {mk.text} (warning only, owner 2026-11-26)"
            off.append(Offender(what, refs, (), mk.pos, None))
            continue
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
    return _metric("M3", count, _verdict(count > 0, accepted + silk_lines > 0), off)


# ---- M4 / M7 bends -------------------------------------------------------------------------

_M4_KINDS = (
    ("non_octilinear", "non_octilinear"),
    ("ninety", "ninety"),
    ("acute", "acute"),
    ("off_angle", "off_angle"),
)  # round 2 (§22.1 a): an off-pad 90° T-junction is no M4 failure (M15 judges junctions)


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
        # duplicate pad numbers: the same-number pad on the supply net nearest the cap (the
        # pad classify chose), else the nearest same-number pad
        same = sorted((q for q in (chip.pads if chip else ()) if q.number == d.pin), key=_pad_key)
        pin = [q for q in same if q.net == d.supply_net] or same
        if cap is not None and chip is not None and sup and pin:
            s0 = sup[0]
            best = min(pin, key=lambda q: octi_dist((s0.x, s0.y), (q.x, q.y)))
            items.append(("decoupling", cap, chip, s0, best))
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
        # the `misc` block (no anchor) collects unrelated leftovers: not a group
        blocks = [(b.name, b.refs) for b in c.cls.blocks if b.anchor is not None]
    off: list[Offender] = []
    worst: float | None = None
    # courtyard bounding boxes once, bucketed: each block only tests the parts near its hull
    others = [p for p in c.m.parts if len(p.courtyard) >= 3]
    boxes = [_bbox(p.courtyard) for p in others]
    grid = _Grid(max(BUCKET_MM, 10.0))
    for i, b in enumerate(boxes):
        grid.add(i, b)
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
        for i in grid.query(hb):
            p = others[i]
            if p.ref in inside or p.side != side:
                continue
            if _bbox_hit(boxes[i], hb) and c.placed(p) and polygons_overlap(p.courtyard, hull):
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


def _outside_discs(
    a: Point, b: Point, discs: Sequence[tuple[Point, float]]
) -> list[tuple[Point, Point]]:
    """The pieces of segment ab outside the union of the discs (centre, radius)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    aa = dx * dx + dy * dy
    if aa <= _EPS:
        inside = any(math.dist(a, cc) <= r for cc, r in discs)
        return [] if inside else [(a, b)]
    spans: list[tuple[float, float]] = []
    for (cx, cy), r in discs:
        fx, fy = a[0] - cx, a[1] - cy
        bb = 2 * (fx * dx + fy * dy)
        disc = bb * bb - 4 * aa * (fx * fx + fy * fy - r * r)
        if disc <= 0:
            continue
        sq = math.sqrt(disc)
        t0, t1 = max(0.0, (-bb - sq) / (2 * aa)), min(1.0, (-bb + sq) / (2 * aa))
        if t1 > t0:
            spans.append((t0, t1))
    out: list[tuple[Point, Point]] = []
    reach = 0.0

    def at(t: float) -> Point:
        return (a[0] + t * dx, a[1] + t * dy)

    for t0, t1 in sorted(spans):
        if t0 > reach + 1e-9:
            out.append((at(reach), at(t0)))
        reach = max(reach, t1)
    if reach < 1.0 - 1e-9:
        out.append((at(reach), b))
    return out


def _hit_params(a: Point, b: Point, c: Point, d: Point) -> list[float]:
    """Parameters on ab (0..1) where segment cd meets it: the crossing point, or the
    projections of c and d when the segments are collinear."""
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    qx, qy = c[0] - a[0], c[1] - a[1]
    den = rx * sy - ry * sx
    rr = rx * rx + ry * ry
    if rr <= _EPS * _EPS:
        return []
    if abs(den) > 1e-12 * max(1.0, rr):
        t, u = (qx * sy - qy * sx) / den, (qx * ry - qy * rx) / den
        return [t] if -_EPS <= t <= 1 + _EPS and -_EPS <= u <= 1 + _EPS else []
    if abs(qx * ry - qy * rx) > _EPS * math.sqrt(rr):
        return []  # parallel, not collinear
    return [(qx * rx + qy * ry) / rr, ((d[0] - a[0]) * rx + (d[1] - a[1]) * ry) / rr]


_Edge = tuple[float, float, float, float, int]  # x0 < x1: (x0, y0, x1, y1, polygon id)


def _edge_y(e: _Edge, x: float) -> float:
    return e[1] + (e[3] - e[1]) * (x - e[0]) / (e[2] - e[0])


def _covered(active: Sequence[_Edge], x: float) -> float:
    """Length of the vertical line at `x` inside the union of the polygons whose (non-vertical)
    edges are `active`: even-odd intervals per polygon (outer minus holes), merged."""
    ys: dict[int, list[float]] = defaultdict(list)
    for e in active:
        ys[e[4]].append(_edge_y(e, x))
    spans: list[tuple[float, float]] = []
    for v in ys.values():
        v.sort()
        spans += zip(v[::2], v[1::2], strict=False)
    total, reach = 0.0, -math.inf
    for lo, hi in sorted(spans):
        if hi > reach:
            total += hi - max(lo, reach)
            reach = hi
    return total


def _union_area(polys: Sequence[tuple[Sequence[Point], Sequence[Sequence[Point]]]]) -> float:
    """Exact area of the union of polygons with holes (outer ring minus its holes), by a
    vertical slab sweep: between consecutive vertex x-coordinates and the x of every edge
    crossing inside a slab the covered length is linear in x, so it is the width times the
    covered length at the mid-line."""
    edges: list[_Edge] = []
    for pid, (outer, holes) in enumerate(polys):
        for ring in (outer, *holes):
            if len(ring) < 3:
                continue
            for a, b in _ring_edges(ring):
                if a[0] != b[0]:  # a vertical edge covers no slab interior
                    lo, hi = (a, b) if a[0] < b[0] else (b, a)
                    edges.append((lo[0], lo[1], hi[0], hi[1], pid))
    edges.sort()
    xs = sorted({e[0] for e in edges} | {e[2] for e in edges})
    area, k = 0.0, 0
    active: list[_Edge] = []
    for xa, xb in itertools.pairwise(xs):
        while k < len(edges) and edges[k][0] <= xa:
            active.append(edges[k])
            k += 1
        active = [e for e in active if e[2] > xa]  # every end is in xs: e spans [xa, xb]
        if not active:
            continue
        ends = [(_edge_y(e, xa), _edge_y(e, xb)) for e in active]
        cuts = {xa, xb}
        rights = [ends[i][1] for i in sorted(range(len(active)), key=ends.__getitem__)]
        if any(r0 > r1 for r0, r1 in itertools.pairwise(rights)):  # edges cross in the slab
            for i, j in itertools.combinations(range(len(active)), 2):
                d0, d1 = ends[i][0] - ends[j][0], ends[i][1] - ends[j][1]
                if active[i][4] != active[j][4] and d0 * d1 < 0:
                    cuts.add(xa + (xb - xa) * d0 / (d0 - d1))
        for x0, x1 in itertools.pairwise(sorted(cuts)):
            area += (x1 - x0) * _covered(active, (x0 + x1) / 2)
    return area


class _PlaneVoids:
    """The voids of one plane layer, from every filled ground polygon on it: the region
    inside the outline outside every filled polygon ("outer") and every hole that is not
    antipads / thermal gaps (its boundary lies within the union of the discs of the vias /
    plated pads near it and its area is at most those discs' total area)."""

    def __init__(
        self,
        filled: Sequence[tuple[str, Sequence[Point], Sequence[Sequence[Point]]]],
        items: Sequence[tuple[Point, float]],
        outline: _Ring,
    ) -> None:
        self.outline = outline
        igrid = _Grid()
        for i, ((x, y), reach) in enumerate(items):
            igrid.add(i, (x - reach, y - reach, x + reach, y + reach))
        self.polys: list[tuple[_Ring, list[tuple[str, _Ring]]]] = []
        self.edges: list[tuple[Point, Point]] = []
        for label, outer, holes in filled:
            if len(outer) < 3:
                continue
            voids: list[tuple[str, _Ring]] = []
            for j, hole in enumerate(holes):
                if len(hole) < 3:
                    continue
                ring = _Ring(hole)
                discs = [
                    items[k]
                    for k in igrid.query(ring.bbox)
                    if _bbox_hit(_bbox((items[k][0],), items[k][1]), ring.bbox)
                ]
                if (
                    discs
                    and polygon_area(hole) <= sum(math.pi * r * r for _, r in discs) + _EPS
                    and all(_disc_covers(a, b, discs) for a, b in _ring_edges(hole))
                ):
                    continue  # via / plated pad antipads (merged) or thermal-relief gaps
                voids.append((f"{label} hole {j}", ring))
                self.edges += _ring_edges(hole)
            self.polys.append((_Ring(outer), voids))
            self.edges += _ring_edges(outer)
        self.grid = _Grid()
        for k, (a, b) in enumerate(self.edges):
            self.grid.add(k, _bbox((a, b)))
        # fill polygons (coarse cells: they are large) and void rings bucketed by bbox
        self.poly_grid = _Grid(max(BUCKET_MM, 10.0))
        self.void_grid = _Grid()
        self.void_list: list[tuple[int, str, _Ring]] = []
        for i, (fill, rings) in enumerate(self.polys):
            self.poly_grid.add(i, _grow(fill.bbox, _EPS))  # Ring.contains allows _EPS
            for vid, void in rings:
                self.void_grid.add(len(self.void_list), _grow(void.bbox, _EPS))
                self.void_list.append((i, vid, void))

    def void_at(self, p: Point) -> str | None:
        """The void id containing `p`, None when `p` is covered or outside the outline."""
        if not self.outline.contains(p):
            return None
        box = (p[0], p[1], p[0], p[1])
        near = [self.void_list[k] for k in self.void_grid.query(box)]  # in polygon order
        hole: str | None = None
        for i in self.poly_grid.query(box):
            if not self.polys[i][0].contains(p):
                continue
            hit = next((vid for j, vid, ring in near if j == i and ring.contains(p)), None)
            if hit is None:
                return None
            hole = hole or hit
        return hole or "outer"

    def crossed(self, a: Point, b: Point) -> set[str]:
        """The voids segment ab enters: ab is cut at every void / fill boundary it meets and
        its ends and the middle of every piece are located (a seam between two fills that
        touch is no void)."""
        ts = {0.0, 1.0}
        for k in self.grid.query(_bbox((a, b))):
            ts.update(min(1.0, max(0.0, t)) for t in _hit_params(a, b, *self.edges[k]))
        cuts = sorted(ts)
        probes = [0.0, 1.0] + [(t0 + t1) / 2 for t0, t1 in itertools.pairwise(cuts)]
        out: set[str] = set()
        for t in probes:
            vid = self.void_at((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
            if vid is not None:
                out.add(vid)
        return out


def _m11(c: _Ctx) -> Metric:
    inner = c.m.copper_layers[1:-1]
    zones = [z for z in c.m.zones if not z.rule_area and z.net and c.kind(z.net) == "ground"]
    # spec §12: fill ratio and voids per plane LAYER, over every ground zone on it
    plane_layers = [la for la in inner if any(la in z.layers for z in zones)]
    if not plane_layers or c.outline is None:
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
    # round 2 (§22.5 M11): a track's piece inside the antipad disc of its OWN net's via or
    # plated pad is no void crossing (same reach as the antipad test above)
    own: dict[str, list[tuple[Point, float]]] = defaultdict(list)
    for v in c.m.vias:
        own[v.net].append((v.at, v.diameter / 2 + ANTIPAD_MARGIN_MM))
    for fp in c.m.parts:
        for fq in fp.pads:
            if fq.drill is not None and _copper(fq):
                own[fq.net].append(((fq.x, fq.y), math.hypot(fq.w, fq.h) / 2 + ANTIPAD_MARGIN_MM))
    own_grid: dict[str, _Grid] = {}
    for net, discs in own.items():
        g = own_grid[net] = _Grid()
        for k, (at, r) in enumerate(discs):
            g.add(k, _bbox((at,), r))
    off: list[Offender] = []
    worst: float | None = None
    layers = list(c.m.copper_layers)
    for layer in plane_layers:
        on_layer = [z for z in zones if layer in z.layers]
        nets = tuple(sorted({z.net for z in on_layer}, key=natural_key))
        filled: list[tuple[str, Sequence[Point], Sequence[Sequence[Point]]]] = []
        area = 0.0
        # zones whose polygons are exactly this layer's fill: (bbox, area, polygons)
        exact: list[tuple[BBox, float, list[tuple[Sequence[Point], Sequence[Sequence[Point]]]]]]
        exact = []
        for z in on_layer:
            holes = list(z.filled_holes) + [()] * (len(z.filled) - len(z.filled_holes))
            per_layer = len(z.filled_layers) == len(z.filled) > 0
            mine = [
                (f"{z.name} {i}", poly, holes[i])
                for i, poly in enumerate(z.filled)
                if not per_layer or z.filled_layers[i] == layer
            ]
            if not mine and (per_layer or z.filled_area_mm2 <= 0):
                off.append(
                    Offender(f"zone not filled: {z.name} on {layer}", (), (z.net,), None, None)
                )
                continue
            if mine:
                zarea = sum(polygon_area(p) - sum(polygon_area(h) for h in hs) for _, p, hs in mine)
            else:
                zarea = z.filled_area_mm2
            filled += mine
            n_cu = sum(1 for la in z.layers if la in layers)
            if mine and (per_layer or n_cu == 1):
                box = _bbox(pt for _, p, _ in mine for pt in p)
                exact.append((box, zarea, [(p, hs) for _, p, hs in mine]))
                continue
            # without per-layer polygons the zone's total is shared evenly by its copper layers
            area += zarea / (1 if per_layer else max(1, n_cu))
        overlap = any(_bbox_hit(a[0], b[0]) for a, b in itertools.combinations(exact, 2))
        if overlap:  # overlapping zones (same or other ground net): count shared copper once
            area += _union_area([poly for e in exact for poly in e[2]])
        else:
            area += sum(e[1] for e in exact)
        ratio = min(1.0, area / board_area) if board_area > 0 else 0.0
        worst = ratio if worst is None else min(worst, ratio)
        if ratio < REVIEW["M11_fill"]:
            off.append(Offender(f"fill {layer}: {ratio:.3f}", (), nets, None, _r(ratio)))
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
            dg = own_grid.get(net)
            discs = [own[net][k] for k in dg.query(_bbox((p, q)))] if dg is not None else []
            crossed: set[str] = set()
            for s0, s1 in _outside_discs(p, q, discs) if discs else [(p, q)]:
                crossed |= voids.crossed(s0, s1)
            for vid in sorted(crossed):
                if (net, vid) in seen:
                    continue
                seen.add((net, vid))
                mid = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
                what = f"void crossing: {net} over {layer} {vid}"
                off.append(Offender(what, (), (net,), mid, None))
    value = None if worst is None else _r(worst)
    return _metric("M11", value, _verdict(False, bool(off)), off)


# ---- M12 widths and pairs ------------------------------------------------------------------


EFFECTIVE_PREFIX = "Effective for net:"  # KiCad's name of a pattern-assigned net's class


def _class_width(
    classes: dict[str, NetClassInfo],
    name: str,
    net: str = "",
    patterned: Sequence[NetClassInfo] = (),
) -> float | None:
    """The minimum track width of net class `name`. A composite effective class ("Default,
    Power", as KiCad stores a net in several classes) takes the widest track_width among its
    known constituents: KiCad's effective class takes each field from the highest-priority
    constituent, and the widest is the conservative choice for a minimum-width check.
    Unknown constituents are ignored. With none known, the classes whose patterns match
    `net` (KiCad wildcards, case-sensitive) plus Default apply - KiCad names such a net's
    class "Effective for net: <net>" (P2 residual; `pcb_route` sizes by the same rule);
    an "Effective for net" name without a matching pattern means Default. None when
    nothing applies."""
    widths = [classes[n].track_width for n in (s.strip() for s in name.split(",")) if n in classes]
    if widths:
        return max(widths)
    widths = [nc.track_width for nc in patterned if any(fnmatchcase(net, p) for p in nc.patterns)]
    if (widths or name.startswith(EFFECTIVE_PREFIX)) and "Default" in classes:
        widths.append(classes["Default"].track_width)
    return max(widths) if widths else None


def _inside_box(pts: Iterable[Point], zone: Sequence[Point]) -> bool:
    """Every point inside the axis-aligned box `zone` (an `escape_zone`; boundary inside)."""
    x0, y0, x1, y1 = _bbox(zone)
    return all(x0 - _EPS <= x <= x1 + _EPS and y0 - _EPS <= y <= y1 + _EPS for x, y in pts)


def _necked(
    c: _Ctx,
    net: str,
    layer: str,
    width: float,
    class_w: float,
    pts: Sequence[Point],
    min_track: float,
) -> bool:
    """Round 2 (§22.5 M12): a narrow piece of `net` on `layer` is a neck-down when it lies
    entirely in the escape zone of a part with a pad of `net` on `layer` whose neck width
    is below the class width and the piece is at least that neck width wide."""
    for zone, part in c.escape_zones(net, _bbox(pts)):
        if not _inside_box(pts, zone):
            continue
        for q in part.pads:
            if q.net != net or not _copper(q) or not _pad_on_layer(q, layer):
                continue
            neck = neck_width(q, class_w, min_track)
            if neck < class_w - 1e-9 and width >= neck - M12_WIDTH_TOL - 1e-9:
                return True
    return False


def neck_checker(
    model: BoardModel, cls: Classification, preset: FabPreset | None = None
) -> Callable[[Track, float], bool]:
    """`check(track, class_width)`: the track is a legal neck-down by M12's rule (`_necked`;
    preset `jlcpcb-4l` when none) - the fill keeps such a track narrow (R4, `_widen`)."""
    c = _Ctx(model, cls, BoardDoc(), preset)
    min_track = (preset or PRESETS["jlcpcb-4l"]).min_track

    def check(t: Track, class_w: float) -> bool:
        return _necked(c, t.net, t.layer, t.width, class_w, (t.start, t.end), min_track)

    return check


def _class_clearance(
    classes: dict[str, NetClassInfo],
    name: str,
    net: str = "",
    patterned: Sequence[NetClassInfo] = (),
) -> float | None:
    """The clearance of net class `name`, resolved like `_class_width`: the largest among
    its known constituents, else the classes whose patterns match `net` plus Default."""
    vals = [classes[n].clearance for n in (s.strip() for s in name.split(",")) if n in classes]
    if vals:
        return max(vals)
    vals = [nc.clearance for nc in patterned if any(fnmatchcase(net, p) for p in nc.patterns)]
    if (vals or name.startswith(EFFECTIVE_PREFIX)) and "Default" in classes:
        vals.append(classes["Default"].clearance)
    return max(vals) if vals else None


def _m12(c: _Ctx) -> Metric:
    classes = {nc.name: nc for nc in c.m.net_classes}
    class_of = dict(c.m.net_class_of)
    need: dict[str, float | None] = {}
    off: list[Offender] = []
    # (net, layer, width, offender anchor pair, points that must lie in an escape zone)
    items: list[tuple[str, str, float, tuple[Point, Point], tuple[Point, ...]]] = [
        (t.net, t.layer, t.width, (t.start, t.end), (t.start, t.end)) for t in c.m.tracks
    ]
    # an arc as points along it: its bulge may leave an escape zone its ends lie in
    items += [
        (a.net, a.layer, a.width, (a.start, a.mid), tuple(_arc_points(a.start, a.mid, a.end)))
        for a in c.m.arcs
    ]
    patterned = [nc for nc in c.m.net_classes if nc.patterns]
    min_track = (c.preset or PRESETS["jlcpcb-4l"]).min_track
    for net, layer, width, (p, q), pts in items:
        name = class_of.get(net, "Default")
        key = net if name.startswith(EFFECTIVE_PREFIX) or patterned else name
        if key not in need:
            need[key] = _class_width(classes, name, net, patterned)
        tw = need[key]
        if tw is None or width >= tw - M12_WIDTH_TOL - 1e-9:
            continue
        if _necked(c, net, layer, width, tw, pts, min_track):
            continue
        mid = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
        what = f"track_width {net}: {width:g} < {tw:g} ({name})"
        off.append(Offender(what, (), (net,), mid, _r(tw - width)))
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
    if round(t.angle) % 180 == 90:  # P7: a vertical text (TextItem.angle)
        w, h = h, w
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
            plated = _copper(q) and not q.npth  # KiCad lists *.Cu on NPTH pads too (R5)
            kind, limit = ("pth_drill", pr.min_drill) if plated else ("npth_drill", pr.min_npth)
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
        """Every copper SMD pad has paste: on the pad itself or from a paste-only pad (no
        copper layer) of the same part whose centre lies inside it (KiCad's QFN/DFN exposed
        pads get their paste from such sub-pads). Fiducials, test points and solder jumpers
        (ref `JPn` or a `Jumper*` library, with no pasted pad at all) are paste-free by design."""
        for p, q in self.pads:
            if not q.smd or self.c.cls.part_class.get(p.ref) in ("fiducial", "test_point"):
                continue
            if _solder_jumper(p):
                continue
            for cu, paste in (("F.Cu", "F.Paste"), ("B.Cu", "B.Paste")):
                if cu in q.layers and paste not in q.layers and not _sub_pasted(p, q, paste):
                    detail = f"{_pin(q)} has no {paste}"
                    self.add("paste_missing", detail, (q.x, q.y), None, (p.ref,), (q.net,))


_PASTE = ("F.Paste", "B.Paste")
_JUMPER_REF = re.compile(r"JP\d")


def _solder_jumper(p: Part) -> bool:
    """A solder jumper (ref `JPn` or library nickname `Jumper*`, case-insensitive) none of
    whose pads has paste: KiCad's SolderJumper pads are intentionally paste-free."""
    nick = p.lib_id.partition(":")[0]
    named = bool(_JUMPER_REF.match(p.ref)) or nick.casefold().startswith("jumper")
    return named and not any(layer in q.layers for q in p.pads for layer in _PASTE)


def _sub_pasted(p: Part, q: Pad, paste: str) -> bool:
    """A paste-only pad (`paste` among its layers, no copper) of part `p` has its centre
    inside pad `q`."""
    return any(paste in s.layers and not _copper(s) and _in_pad((s.x, s.y), q) for s in p.pads)


_WORD = re.compile(r"\w+")


class _Conditions:
    """The custom rule conditions of the `.kicad_dru`, indexed once: a ref is named in one
    when it occurs there not preceded by a word character or `.` and not followed by a word
    character. For an all-word ref that is exactly a maximal word run not after a `.` (the
    token set); other refs fall back to the regex (memoised)."""

    def __init__(self, conditions: Sequence[str]) -> None:
        self.conditions = tuple(conditions)
        self.tokens = {
            m.group()
            for cond in self.conditions
            for m in _WORD.finditer(cond)
            if m.start() == 0 or cond[m.start() - 1] != "."
        }
        self.other: dict[str, bool] = {}

    def names(self, ref: str) -> bool:
        if _WORD.fullmatch(ref):
            return ref in self.tokens
        if ref not in self.other:
            pat = re.compile(rf"(?<![\w.]){re.escape(ref)}(?!\w)")
            self.other[ref] = any(pat.search(cond) for cond in self.conditions)
        return self.other[ref]


def _excepted(o: Offender, conditions: _Conditions) -> bool:
    """Every ref of `o` is named in some custom rule condition of the `.kicad_dru`."""
    return bool(o.refs) and all(conditions.names(r) for r in o.refs)


def _m14(c: _Ctx) -> Metric:
    if c.preset is None:
        return _metric("M14", None, "n/a")
    fab = _Fab(c, c.preset)
    fab.rules()
    fab.items()
    fab.edges()
    fab.silk()
    fab.paste()
    conditions = _Conditions(_CONDITION.findall(c.m.dru_text))
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


# ---- M15 / M16 junctions and pad links (round 2, §22.5) -----------------------------------


def _m15(c: _Ctx) -> Metric:
    off = [
        Offender("y_junction", (), (net,), pt, None)
        for net, rep in c.bends().items()
        for pt in rep.y_junctions
    ]
    return _metric("M15", len(off), _verdict(M15_HARD and bool(off), bool(off)), off)


class _Blockers:
    """Copper and keepouts that can block a straight corridor: every copper pad and via
    (bucketed), keepout zones (tracks) and footprint rule areas (every layer)."""

    def __init__(self, c: _Ctx) -> None:
        self.pads = [q for p in c.m.parts for q in p.pads if _copper(q)]
        self.vias = list(c.m.vias)
        self.pad_grid, self.via_grid = _Grid(), _Grid()
        for i, q in enumerate(self.pads):
            self.pad_grid.add(i, _bbox(_pad_poly(q)))
        for i, v in enumerate(self.vias):
            self.via_grid.add(i, _bbox((v.at,), v.diameter / 2))
        self.keepouts: list[tuple[_Ring, frozenset[str] | None]] = []
        for z in c.m.zones:
            if z.rule_area and z.keepout & {"tracks", "copper"} and len(z.outline) >= 3:
                layers = None if not z.layers or "*.Cu" in z.layers else frozenset(z.layers)
                self.keepouts.append((_Ring(z.outline), layers))
        for p in c.m.parts:
            self.keepouts += [(_Ring(ra), None) for ra in p.rule_areas if len(ra) >= 3]

    def free(self, net: str, layer: str, s: Point, e: Point, reach: float) -> bool:
        """No other-net pad on `layer` or via within `reach` of segment se, and no keepout
        on `layer` touched by the segment grown by `reach`."""
        box = _bbox((s, e), reach)
        ring = _seg_rect(s, e, reach)
        for i in self.pad_grid.query(box):
            q = self.pads[i]
            if q.net != net and _pad_on_layer(q, layer) and polygons_overlap(ring, _pad_poly(q)):
                return False
        for i in self.via_grid.query(box):
            v = self.vias[i]
            if v.net != net and _pt_seg(v.at, s, e) < reach + v.diameter / 2 - _EPS:
                return False
        for kr, layers in self.keepouts:
            if layers is not None and layer not in layers:
                continue
            if _bbox_hit(kr.bbox, box) and polygons_overlap(ring, kr.pts):
                return False
        return True


def _seg_rect(s: Point, e: Point, r: float) -> list[Point]:
    """Segment se grown by `r` on every side (a rectangle; se is axis-parallel here)."""
    x0, y0, x1, y1 = _bbox((s, e), r)
    return rect_poly(x0, y0, x1 - x0, y1 - y0)


def _straight_link(
    segs: Sequence[tuple[str, Point, Point, float]], a: Pad, b: Pad, layers: Sequence[str]
) -> bool:
    """The routed path is one straight segment on one of `layers` outside the two pads:
    pieces wholly inside pad a or b dropped, collinear same-layer touching pieces merged
    (an own-net T branch splits a straight track in the net graph)."""
    rest = [
        (la, p, q)
        for la, p, q, _ in segs
        if not ((_in_pad(p, a) and _in_pad(q, a)) or (_in_pad(p, b) and _in_pad(q, b)))
    ]
    merged: list[tuple[str, Point, Point]] = []
    for la, p, q in rest:
        if merged:
            ml, mp, mq = merged[-1]
            if (
                ml == la
                and math.dist(mq, p) <= _JOIN_TOL
                and abs(_cross3(mp, mq, q)) <= 1e-6 * (1.0 + math.dist(mp, mq) * math.dist(mq, q))
            ):
                merged[-1] = (la, mp, q)
                continue
        merged.append((la, p, q))
    return len(merged) == 1 and merged[0][0] in layers


def _runs(
    segs: Sequence[tuple[str, Point, Point, float]],
) -> list[tuple[list[Point], float, float]]:
    """The path's same-layer, end-to-end runs as (points, width of the first piece, width of
    the last piece)."""
    out: list[tuple[list[Point], float, float]] = []
    layer = None
    for la, p, q, w in segs:
        if out and la == layer and math.dist(out[-1][0][-1], p) <= _JOIN_TOL:
            out[-1][0].append(q)
            out[-1] = (out[-1][0], out[-1][1], w)
            continue
        out.append(([p, q], w, w))
        layer = la
    return out


def _pair_text(net: str, a: Pad, b: Pad) -> str:
    return f"{net} {_pin(a)}-{_pin(b)}"


M16Kind = Literal["dogleg", "pad_loop", "pad_jog", "alignable"]


@dataclass(frozen=True)
class M16Defect:
    """One M16 finding: `kind`, the connection (net, pads a / b in MST order), the pad of a
    pad-entry defect (`end`), where, and the shift of an `alignable` pair."""

    kind: M16Kind
    net: str
    a: Pad
    b: Pad
    at: Point
    end: Pad | None = None
    shift: float | None = None
    layer: str | None = None  # a pad-entry defect's run layer (the post-processor's attribution)


def _m16_defects(c: _Ctx, nets: Iterable[str] | None = None) -> Iterator[M16Defect]:
    """M16's findings over every non-ground net (or those of `nets`): placement stage the
    alignable pairs, routed stage doglegs and pad-entry loops / jogs (a pad shared by
    several MST connections with one entry piece reported once)."""
    classes = {nc.name: nc for nc in c.m.net_classes}
    class_of = dict(c.m.net_class_of)
    patterned = [nc for nc in c.m.net_classes if nc.patterns]
    min_track = (c.preset or PRESETS["jlcpcb-4l"]).min_track
    default_clear = dict(c.m.rules).get("min_clearance", 0.2)
    blockers: _Blockers | None = None
    seen: set[tuple[object, ...]] = set()
    routed = c.stage == "routed"
    wanted = None if nets is None else set(nets)
    for net in sorted(n for n in c.pads_by_net if n and c.kind(n) != "ground"):
        if wanted is not None and net not in wanted:
            continue
        pads = c.pads_by_net[net]
        if len(pads) < 2:
            continue
        name = class_of.get(net, "Default")
        class_w = _class_width(classes, name, net, patterned) or min_track
        clear = _class_clearance(classes, name, net, patterned) or default_clear
        for a, b in _mst(pads):
            w = min(neck_width(a, class_w, min_track), neck_width(b, class_w, min_track))
            if not routed:
                gap = _alignable(c, a, b, w)
                if gap is not None:
                    yield M16Defect("alignable", net, a, b, (a.x, a.y), shift=gap)
                continue
            g = c.graph(net)
            nodes = g.path(a, b)
            if nodes is None:
                continue  # unrouted: M2's business, never a dogleg
            segs = g.segments(nodes)
            if not segs:
                continue
            link = straight_link_corridor(
                a,
                b,
                class_w,
                w,
                neck_link_zones(c.parts, a, b, class_w, min_track),
                copper_layers=c.m.copper_layers,
            )
            if link is not None:
                if blockers is None:
                    blockers = _Blockers(c)
                layers, s, e, outside = link
                free = [
                    la
                    for la in layers
                    if blockers.free(net, la, s, e, w / 2 + clear)
                    and all(blockers.free(net, la, p, q, class_w / 2 + clear) for p, q in outside)
                ]
                # a dogleg needs a free corridor; a straight link on ANY shared layer is fine
                if free and not _straight_link(segs, a, b, layers):
                    yield M16Defect("dogleg", net, a, b, _mid_pt(s, e))
            runs = _runs(segs)
            first, last = runs[0], runs[-1]
            run_layer = {id(a): segs[0][0], id(b): segs[-1][0]}
            for end, pts, width in ((a, first[0], first[1]), (b, last[0][::-1], last[2])):
                if not _in_pad(pts[0], end):
                    continue
                rep = analyse_entry(
                    pts, width, entry_zone(end, width), in_pad=functools.partial(_pad_has, end)
                )
                for kind, pts_at in (("pad_loop", rep.loops), ("pad_jog", rep.jogs)):
                    for pt in pts_at:
                        mark = (kind, _NetGraph.pad_node(end), round(pt[0], 4), round(pt[1], 4))
                        if mark in seen:
                            continue
                        seen.add(mark)
                        yield M16Defect(
                            cast(M16Kind, kind), net, a, b, pt, end=end, layer=run_layer[id(end)]
                        )


def m16_defects(
    model: BoardModel,
    cls: Classification,
    *,
    nets: Iterable[str] | None = None,
    preset: FabPreset | None = None,
) -> list[M16Defect]:
    """M16's findings of `model` (B-C7 round 2) restricted to `nets`: the post-processor
    rips the connections with a hard one (R4)."""
    return list(_m16_defects(_Ctx(model, cls, BoardDoc(), preset), nets))


def _m16(c: _Ctx) -> Metric:
    off: list[Offender] = []
    hard = 0
    for d in _m16_defects(c):
        pair = _pair_text(d.net, d.a, d.b)
        if d.kind == "alignable":
            shift = d.shift or 0.0
            off.append(
                Offender(
                    f"alignable {pair}: {shift:.2f} mm off",
                    _refs(d.a, d.b),
                    (d.net,),
                    d.at,
                    _r(shift),
                )
            )
        elif d.kind == "dogleg":
            hard += 1
            off.append(Offender(f"dogleg {pair}", _refs(d.a, d.b), (d.net,), d.at, None))
        else:  # owner 2026-11-27: a pad-entry jog / S-offset is an error like a loop
            hard += 1
            end = d.end if d.end is not None else d.a
            off.append(Offender(f"{d.kind} {_pin(end)} ({pair})", (end.ref,), (d.net,), d.at, None))
    return _metric("M16", hard, _verdict(M16_HARD and hard > 0, bool(off)), off)


def _pad_has(pad: Pad, p: Point) -> bool:
    return _in_pad(p, pad)


def _refs(a: Pad, b: Pad) -> tuple[str, ...]:
    return tuple(sorted({a.ref, b.ref}, key=natural_key))


def _mid_pt(a: Point, b: Point) -> Point:
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)


def _alignable(c: _Ctx, a: Pad, b: Pad, width: float) -> float | None:
    """Placement stage: the shift (mm, <= REVIEW["M16_align_mm"]) that would give two
    adjacent pads of one net - at least one on a two-pad part, sharing a copper layer, no
    straight corridor yet - a corridor for `width`; None otherwise."""
    pa, pb = c.parts.get(a.ref), c.parts.get(b.ref)
    if pa is None or pb is None or a.ref == b.ref:
        return None
    if len(pa.pads) != 2 and len(pb.pads) != 2:
        return None
    need = alignment_need(a, b, width, c.m.copper_layers)
    return need if need else None


def alignment_need(a: Pad, b: Pad, width: float, copper_layers: Sequence[str]) -> float | None:
    """M16's placement-stage geometry for two pads (R2's `align_pairs` uses it too): 0.0 when
    a straight corridor for `width` exists, the missing overlap (0 < need <=
    REVIEW["M16_align_mm"]) when they share a copper layer and are adjacent along an axis,
    else None."""
    if not any(_pad_on_layer(a, la) and _pad_on_layer(b, la) for la in copper_layers):
        return None
    if straight_corridor(a, b, width, copper_layers=copper_layers) is not None:
        return 0.0
    ba, bb = _bbox(_pad_poly(a)), _bbox(_pad_poly(b))
    best: float | None = None
    for ax in (0, 1):
        o = 1 - ax
        gap = max(bb[ax] - ba[ax + 2], ba[ax] - bb[ax + 2])
        if not (_EPS < gap <= ADJ_GAP_MM + _EPS):
            continue
        need = width - (min(ba[o + 2], bb[o + 2]) - max(ba[o], bb[o]))
        if 0.0 < need <= REVIEW["M16_align_mm"] + _EPS and (best is None or need < best):
            best = need
    return best


def alignment_pairs(
    model: BoardModel, cls: Classification
) -> tuple[tuple[str, Pad, Pad, float], ...]:
    """(net, pad a, pad b, corridor width) of every connection M16 checks for alignment at
    the placement stage: the MST connections of each non-ground net between two parts of
    which at least one has exactly two pads (width = the narrower neck width, preset
    `jlcpcb-4l`'s minimum track as at the placement stage). For R2's `align_pairs`."""
    c = _Ctx(model, cls, BoardDoc(), None)
    classes = {nc.name: nc for nc in model.net_classes}
    class_of = dict(model.net_class_of)
    patterned = [nc for nc in model.net_classes if nc.patterns]
    min_track = PRESETS["jlcpcb-4l"].min_track
    out: list[tuple[str, Pad, Pad, float]] = []
    for net in sorted(n for n in c.pads_by_net if n and c.kind(n) != "ground"):
        pads = c.pads_by_net[net]
        if len(pads) < 2:
            continue
        class_w = _class_width(classes, class_of.get(net, "Default"), net, patterned) or min_track
        for a, b in _mst(pads):
            pa, pb = c.parts.get(a.ref), c.parts.get(b.ref)
            if pa is None or pb is None or a.ref == b.ref:
                continue
            if len(pa.pads) != 2 and len(pb.pads) != 2:
                continue
            w = min(neck_width(a, class_w, min_track), neck_width(b, class_w, min_track))
            out.append((net, a, b, w))
    return tuple(out)


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
    """Metrics M1-M16 (spec §12, §22.5) of `model`; `only` restricts the computed metrics (the
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
        "M15": lambda: _m15(c),
        "M16": lambda: _m16(c),
    }
    out = tuple(table[mid]() for mid in METRIC_IDS if only is None or mid in only)
    return MetricsReport(
        metrics=out,
        hard_failures=sum(1 for x in out if x.verdict == "fail"),
        reviews=sum(1 for x in out if x.verdict == "review"),
        stage=c.stage,
    )
