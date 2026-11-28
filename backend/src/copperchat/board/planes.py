"""Pure plane, fanout and stitching candidates (SP4 spec §11, P7 Task 1; no kipy).

`default_planes` is the owner's default zone stack (D1): a solid GND plane on In1.Cu (4
layers; In1.Cu + In4.Cu on 6), GND pours on the outer layers below it (2 layers: the B.Cu
pour is the plane). `fanout_candidates` lists, per SMD pad of a plane net, short exactly
octilinear stub + via positions (8 directions x 2 distances, spike 4A `fanout.py`);
`stitch_candidates` a via grid (plus an optional edge ring) where at least two zones of
the net on different layers would connect. Both drop every position whose via would
overlap a pad (no via-in-pad: it costs extra at JLC, spec call 30), come within the
clearance of other-net copper, break the hole-to-hole rule, sit in a via/track keepout or
too close to the board edge. KiCad's DRC task legalises what is left (the tools).

Pads are modelled as their rotated `w` x `h` rectangle (conservative for round and oval
pads), arcs as the two chords through their mid point. Deterministic: sorted keys, no
randomness."""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from copperchat.board.classify import Classification, primary_ground
from copperchat.board.geom import _bbox_of, _pad_on_layer, _pad_rect, point_in_polygon, rect_poly
from copperchat.board.model import BoardModel, Pad, Point
from copperchat.board.staging import natural_key

__all__ = [
    "DIRECTIONS",
    "SECOND_STEP_MM",
    "ViaCandidate",
    "ZoneSpec",
    "default_planes",
    "edge_clearance",
    "edge_ring_points",
    "fanout_candidates",
    "fanout_pads",
    "hole_to_hole",
    "pad_key",
    "pad_layer",
    "stitch_candidates",
    "vias_spaced",
]

# 0°, 45°, ..., 315° (x right, y down); the stub runs pad centre -> via centre along one.
DIRECTIONS: tuple[tuple[int, int], ...] = (
    (1, 0),
    (1, 1),
    (0, 1),
    (-1, 1),
    (-1, 0),
    (-1, -1),
    (0, -1),
    (1, -1),
)
SECOND_STEP_MM = 0.5  # d2 = d1 + 0.5 (spike)
STITCH_INSET_MM = 0.25  # grid: outline inset by copper_to_edge + via radius + this
RING_INSET_MM = 0.5  # edge ring: outline inset by copper_to_edge + via radius + this
HOLE_TO_HOLE_DEFAULT_MM = 0.25  # KiCad's default when the board has no rule
EDGE_DEFAULT_MM = 0.3  # JLC preset copper-to-edge when the board has no rule
_NM = 1e-6  # mm: positions are rounded to KiCad's 1 nm grid
_CELL = 2.0  # mm: obstacle bucket size


@dataclass(frozen=True)
class ViaCandidate:
    at: Point
    stub: tuple[Point, Point] | None  # fanout: pad centre -> via centre; stitching: None
    pad: tuple[str, str] | None  # (ref, number) of the fanned-out pad
    rank: int  # 0 = preferred
    # R7: no via - the stub ties the pad to a larger same-net pad of its own part (a QFN's
    # ground pin to its exposed pad, which has its own via); `at` = the stub's end there
    tie: bool = False


@dataclass(frozen=True)
class ZoneSpec:
    name: str
    net: str
    layers: tuple[str, ...]
    priority: int


def default_planes(model: BoardModel, cls: Classification) -> tuple[ZoneSpec, ...]:
    """The default zone stack (owner D1, spec §11) on the ground net with the most pads:
    4 layers -> GND_In1 (In1.Cu, 10) + GND_F (F.Cu, 1) + GND_B (B.Cu, 1); 6 layers ->
    GND_In1 + GND_In4 (10) + the two outer pours; 2 layers -> GND_B (B.Cu, 10) + GND_F
    (F.Cu, 1). No ground net or another layer count -> ()."""
    gnd = primary_ground(cls, model)
    if gnd is None:
        return ()
    n = len(model.copper_layers)
    outer = (ZoneSpec("GND_F", gnd, ("F.Cu",), 1), ZoneSpec("GND_B", gnd, ("B.Cu",), 1))
    if n == 2:
        return (ZoneSpec("GND_B", gnd, ("B.Cu",), 10), outer[0])
    if n == 4:
        return (ZoneSpec("GND_In1", gnd, ("In1.Cu",), 10), *outer)
    if n == 6:
        return (
            ZoneSpec("GND_In1", gnd, ("In1.Cu",), 10),
            ZoneSpec("GND_In4", gnd, ("In4.Cu",), 10),
            *outer,
        )
    return ()


# -- board rules -------------------------------------------------------------------------


def hole_to_hole(model: BoardModel) -> float:
    v = dict(model.rules).get("hole_to_hole_min", 0.0)
    return v if v > 0 else HOLE_TO_HOLE_DEFAULT_MM


def edge_clearance(model: BoardModel) -> float:
    v = dict(model.rules).get("copper_edge_clearance", 0.0)
    return v if v > 0 else EDGE_DEFAULT_MM


def pad_layer(pad: Pad) -> str | None:
    """The copper layer of an SMD pad (F.Cu or B.Cu), None for a pad without copper."""
    for layer in ("F.Cu", "B.Cu"):
        if layer in pad.layers:
            return layer
    return None


def vias_spaced(
    a: Point, b: Point, *, via: tuple[float, float], clearance: float, h2h: float
) -> bool:
    """Two new same-net vias far enough apart: copper clearance (neat, never touching)
    and the hole-to-hole rule."""
    return math.dist(a, b) >= max(via[0] + clearance, via[1] + h2h) - 1e-9


# -- distances ---------------------------------------------------------------------------


def _local(p: Point, pad: Pad) -> Point:
    """`p` in the pad's frame (same rotation convention as `geom._in_pad`)."""
    dx, dy = p[0] - pad.x, p[1] - pad.y
    t = math.radians(pad.rot)
    c, s = math.cos(t), math.sin(t)
    return dx * c - dy * s, dx * s + dy * c


def _point_box(p: Point, hw: float, hh: float) -> float:
    dx = max(abs(p[0]) - hw, 0.0)
    dy = max(abs(p[1]) - hh, 0.0)
    return math.hypot(dx, dy)


def _point_seg(p: Point, a: Point, b: Point) -> float:
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    n = dx * dx + dy * dy
    if n <= 0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / n))
    return math.dist(p, (ax + t * dx, ay + t * dy))


def _cross(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _segs_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    d1, d2 = _cross(c, d, a), _cross(c, d, b)
    d3, d4 = _cross(a, b, c), _cross(a, b, d)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 * d2 != 0 and d3 * d4 != 0


def _seg_seg(a: Point, b: Point, c: Point, d: Point) -> float:
    if _segs_cross(a, b, c, d):
        return 0.0
    return min(_point_seg(a, c, d), _point_seg(b, c, d), _point_seg(c, a, b), _point_seg(d, a, b))


def _point_pad(p: Point, pad: Pad) -> float:
    return _point_box(_local(p, pad), pad.w / 2, pad.h / 2)


def _seg_pad(a: Point, b: Point, pad: Pad) -> float:
    la, lb = _local(a, pad), _local(b, pad)
    hw, hh = pad.w / 2, pad.h / 2
    if _point_box(la, hw, hh) == 0 or _point_box(lb, hw, hh) == 0:
        return 0.0
    corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    edges = list(zip(corners, corners[1:] + corners[:1], strict=True))
    if any(_segs_cross(la, lb, c, d) for c, d in edges):
        return 0.0
    return min(
        _point_box(la, hw, hh),
        _point_box(lb, hw, hh),
        *(_point_seg(c, la, lb) for c in corners),
    )


def _poly_dist(p: Point, poly: Sequence[Point]) -> float:
    """Distance from `p` to the boundary of `poly`."""
    return min(_point_seg(p, a, b) for a, b in zip(poly, [*poly[1:], poly[0]], strict=True))


def _seg_hits_poly(a: Point, b: Point, poly: Sequence[Point], r: float) -> bool:
    """The segment (inflated by `r`) touches the polygon's area."""
    if point_in_polygon(a, poly) or point_in_polygon(b, poly):
        return True
    return any(_seg_seg(a, b, c, d) < r for c, d in zip(poly, [*poly[1:], poly[0]], strict=True))


def _extent(pad: Pad, ux: float, uy: float) -> float:
    """Distance from the pad centre to its rectangle's boundary along unit (ux, uy)."""
    lx, ly = _local((pad.x + ux, pad.y + uy), pad)
    tx = (pad.w / 2) / abs(lx) if abs(lx) > 1e-12 else math.inf
    ty = (pad.h / 2) / abs(ly) if abs(ly) > 1e-12 else math.inf
    return min(tx, ty)


# -- obstacles ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Seg:
    net: str
    layer: str
    a: Point
    b: Point
    half: float  # half width


@dataclass(frozen=True)
class _Hole:
    net: str
    at: Point
    r: float  # copper radius (0 for an NPTH)
    drill: float


class _Bucket:
    """Uniform bucket grid of item indices by bounding box."""

    def __init__(self) -> None:
        self.cells: dict[tuple[int, int], list[int]] = {}

    @staticmethod
    def _keys(x0: float, y0: float, x1: float, y1: float) -> Iterator[tuple[int, int]]:
        for gx in range(math.floor(x0 / _CELL), math.floor(x1 / _CELL) + 1):
            for gy in range(math.floor(y0 / _CELL), math.floor(y1 / _CELL) + 1):
                yield gx, gy

    def add(self, i: int, x0: float, y0: float, x1: float, y1: float) -> None:
        for k in self._keys(x0, y0, x1, y1):
            self.cells.setdefault(k, []).append(i)

    def near(self, x0: float, y0: float, x1: float, y1: float) -> list[int]:
        seen: set[int] = set()
        for k in self._keys(x0, y0, x1, y1):
            seen.update(self.cells.get(k, ()))
        return sorted(seen)


class _Obstacles:
    """Everything a new via (and fanout stub) must keep clear of."""

    def __init__(self, model: BoardModel, net: str, reach: float) -> None:
        self.reach = reach  # largest query radius (via radius + clearance + margin)
        self.pads: list[Pad] = [q for p in model.parts for q in p.pads]
        self.pad_b = _Bucket()
        for i, q in enumerate(self.pads):
            r = math.hypot(q.w, q.h) / 2 + reach
            self.pad_b.add(i, q.x - r, q.y - r, q.x + r, q.y + r)
        self.segs: list[_Seg] = []
        for t in model.tracks:
            self.segs.append(_Seg(t.net, t.layer, t.start, t.end, t.width / 2))
        for a in model.arcs:
            self.segs.append(_Seg(a.net, a.layer, a.start, a.mid, a.width / 2))
            self.segs.append(_Seg(a.net, a.layer, a.mid, a.end, a.width / 2))
        self.seg_b = _Bucket()
        for i, s in enumerate(self.segs):
            r = s.half + reach
            self.seg_b.add(
                i,
                min(s.a[0], s.b[0]) - r,
                min(s.a[1], s.b[1]) - r,
                max(s.a[0], s.b[0]) + r,
                max(s.a[1], s.b[1]) + r,
            )
        self.holes: list[_Hole] = [_Hole(v.net, v.at, v.diameter / 2, v.drill) for v in model.vias]
        for q in self.pads:  # pad copper is checked as a pad; the hole for hole-to-hole
            if q.drill is not None and q.drill > 0:
                self.holes.append(_Hole(q.net, (q.x, q.y), 0.0, q.drill))
        self.hole_b = _Bucket()
        for i, h in enumerate(self.holes):
            r = max(h.r, h.drill / 2) + reach
            self.hole_b.add(i, h.at[0] - r, h.at[1] - r, h.at[0] + r, h.at[1] + r)
        # keepouts: board rule areas forbidding vias / tracks (layers kept), footprint
        # rule areas as both on every layer (the model has no type for them, as route.py)
        self.via_keepouts: list[tuple[Point, ...]] = []
        self.track_keepouts: list[tuple[frozenset[str], tuple[Point, ...]]] = []
        for z in model.zones:
            if not z.rule_area or len(z.outline) < 3:
                continue
            if "vias" in z.keepout or "copper" in z.keepout:
                self.via_keepouts.append(z.outline)
            if "tracks" in z.keepout or "copper" in z.keepout:
                self.track_keepouts.append((frozenset(z.layers), z.outline))
        self.fp_keepouts: list[tuple[str, tuple[Point, ...]]] = [
            (p.ref, ra) for p in model.parts for ra in p.rule_areas if len(ra) >= 3
        ]
        self.net = net

    def _pads_near(self, p: Point) -> Iterable[Pad]:
        return (self.pads[i] for i in self.pad_b.near(p[0], p[1], p[0], p[1]))

    def via_ok(
        self,
        at: Point,
        *,
        via: tuple[float, float],
        clearance: float,
        h2h: float,
        skip_keepouts_of: str | None = None,
    ) -> bool:
        r = via[0] / 2
        for q in self._pads_near(at):
            d = _point_pad(at, q)
            if d < r - 1e-9:
                return False  # no via-in-pad, also not in a same-net pad
            if q.net != self.net and d < r + clearance - 1e-9:
                return False
        for i in self.seg_b.near(at[0], at[1], at[0], at[1]):
            s = self.segs[i]
            if s.net != self.net and _point_seg(at, s.a, s.b) < r + s.half + clearance - 1e-9:
                return False
        for i in self.hole_b.near(at[0], at[1], at[0], at[1]):
            h = self.holes[i]
            d = math.dist(at, h.at)
            if d < (via[1] + h.drill) / 2 + h2h - 1e-9:
                return False
            reach = max(h.r, h.drill / 2)  # via copper, or the bare hole of an NPTH
            if h.net != self.net and d < r + reach + clearance - 1e-9:
                return False
            if d < r + reach - 1e-9:
                return False  # never on top of another via
        for poly in self.via_keepouts:
            if point_in_polygon(at, poly) or _poly_dist(at, poly) < r:
                return False
        for ref, poly in self.fp_keepouts:
            if ref == skip_keepouts_of:
                continue
            if point_in_polygon(at, poly) or _poly_dist(at, poly) < r:
                return False
        return True

    def stub_ok(
        self,
        a: Point,
        b: Point,
        layer: str,
        *,
        half: float,
        clearance: float,
        own: Pad,
        skip_keepouts_of: str | None = None,
    ) -> bool:
        x0, y0 = min(a[0], b[0]), min(a[1], b[1])
        x1, y1 = max(a[0], b[0]), max(a[1], b[1])
        for i in self.pad_b.near(x0, y0, x1, y1):
            q = self.pads[i]
            if q is own or q.net == self.net or not _pad_on_layer(q, layer):
                continue
            if _seg_pad(a, b, q) < half + clearance - 1e-9:
                return False
        for i in self.seg_b.near(x0, y0, x1, y1):
            s = self.segs[i]
            if s.layer != layer or s.net == self.net:
                continue
            if _seg_seg(a, b, s.a, s.b) < half + s.half + clearance - 1e-9:
                return False
        for i in self.hole_b.near(x0, y0, x1, y1):
            h = self.holes[i]
            if (
                h.net != self.net
                and _point_seg(h.at, a, b) < half + max(h.r, h.drill / 2) + clearance - 1e-9
            ):
                return False
        for layers, poly in self.track_keepouts:
            if layer in layers and _seg_hits_poly(a, b, poly, half):
                return False
        for ref, poly in self.fp_keepouts:
            if ref != skip_keepouts_of and _seg_hits_poly(a, b, poly, half):
                return False
        return True


def _inside_outline(model: BoardModel, at: Point, inset: float) -> bool:
    """`at` inside the board outline with at least `inset` to its edge."""
    o = model.outline
    if o.kind == "none":
        return False
    if o.kind == "rect":
        poly: Sequence[Point] = rect_poly(o.x, o.y, o.w, o.h, o.corner_radius)
    else:
        poly = o.polygon
    if len(poly) < 3 or not point_in_polygon(at, poly):
        return False
    return _poly_dist(at, poly) >= inset - 1e-9


def _snap(v: float) -> float:
    return round(v / _NM) * _NM


# -- fanout ------------------------------------------------------------------------------


def fanout_pads(model: BoardModel, net: str) -> tuple[list[Pad], list[Pad]]:
    """(to fan out, already connected) SMD copper pads of `net`, natural order. A pad is
    already connected when a same-net track ends in it or a same-net via lies in it (an
    earlier fanout, a route); THT pads reach the planes through their hole."""
    points = [pt for t in model.tracks if t.net == net for pt in (t.start, t.end)]
    points += [pt for a in model.arcs if a.net == net for pt in (a.start, a.end)]
    points += [v.at for v in model.vias if v.net == net]
    bucket = _Bucket()
    for i, (x, y) in enumerate(points):
        bucket.add(i, x, y, x, y)
    todo: list[Pad] = []
    done: list[Pad] = []
    for part in sorted(model.parts, key=lambda p: natural_key(p.ref)):
        for q in sorted(part.pads, key=lambda q: natural_key(q.number)):
            if q.net != net or not q.smd or pad_layer(q) is None:
                continue
            reach = math.hypot(q.w, q.h) / 2
            ids = bucket.near(q.x - reach, q.y - reach, q.x + reach, q.y + reach)
            near = [points[i] for i in ids]
            if any(_point_pad(p, q) <= 1e-6 for p in near):
                done.append(q)
            else:
                todo.append(q)
    return todo, done


def fanout_candidates(
    model: BoardModel,
    net: str,
    *,
    via: tuple[float, float],
    width: float,
    clearance: float,
    max_stub: float = 1.0,
) -> dict[tuple[str, str], tuple[ViaCandidate, ...]]:
    """Per SMD pad of `net` still to fan out (`fanout_pads`): its stub + via candidates,
    preferred first (rank). 8 directions x 2 distances d1 = pad extent along the direction
    + clearance + via radius, d2 = d1 + 0.5 mm; the stub (pad centre -> via centre) is
    exactly 0/45/90/... degrees on the nm grid. Dropped: the stub outside the pad longer
    than `max_stub`, a via overlapping any pad, other-net copper within `clearance` of the
    via or the stub, the hole-to-hole rule, keepouts, the outline inset by copper-to-edge
    + via radius. Order: d1 before d2, then the direction pointing most away from the
    footprint centre. A pad with no candidate maps to ()."""
    todo, _ = fanout_pads(model, net)
    r = via[0] / 2
    obs = _Obstacles(model, net, reach=r + clearance + width + 1.0)
    h2h, edge = hole_to_hole(model), edge_clearance(model)
    centre = {p.ref: (p.x, p.y) for p in model.parts}
    out: dict[tuple[str, str], tuple[ViaCandidate, ...]] = {}
    for q in todo:
        layer = pad_layer(q)
        assert layer is not None
        cx, cy = centre[q.ref]
        away = (q.x - cx, q.y - cy)
        n_away = math.hypot(*away)
        own_area = _own_area(model, q)
        found: list[tuple[tuple[int, float, int], Point, tuple[Point, Point]]] = []
        for di, (dx, dy) in enumerate(DIRECTIONS):
            norm = math.hypot(dx, dy)
            ux, uy = dx / norm, dy / norm
            ext = _extent(q, ux, uy)
            d1 = ext + clearance + r
            score = -(ux * away[0] + uy * away[1]) / n_away if n_away > 1e-9 else 0.0
            for step, dist in enumerate((d1, d1 + SECOND_STEP_MM)):
                if dist - ext > max_stub + 1e-9:
                    continue
                if dx and dy:
                    # down to the nm grid: one offset for both axes keeps the stub at
                    # exactly 45° and never longer than `dist`
                    off = math.floor(dist / math.sqrt(2) / _NM) * _NM
                    at = (_snap(q.x + dx * off), _snap(q.y + dy * off))
                else:
                    at = (_snap(q.x + dx * dist), _snap(q.y + dy * dist))
                if not _inside_outline(model, at, edge + r):
                    continue
                if not obs.via_ok(
                    at, via=via, clearance=clearance, h2h=h2h, skip_keepouts_of=own_area
                ):
                    continue
                stub = ((q.x, q.y), at)
                if not obs.stub_ok(
                    stub[0],
                    at,
                    layer,
                    half=width / 2,
                    clearance=clearance,
                    own=q,
                    skip_keepouts_of=own_area,
                ):
                    continue
                found.append(((step, round(score, 9), di), at, stub))
        found.sort(key=lambda f: f[0])
        key = pad_key(q, out)
        cands = [
            ViaCandidate(at=at, stub=stub, pad=key, rank=i) for i, (_, at, stub) in enumerate(found)
        ]
        for end in _tie_ends(model, q, layer, obs, width=width, clearance=clearance):
            cands.append(
                ViaCandidate(at=end, stub=((q.x, q.y), end), pad=key, rank=len(cands), tie=True)
            )
        out[key] = tuple(cands)
    return out


TIE_MAX_GAP_MM = 1.0  # R7: a tie stub bridges at most this much between the two pads


def _tie_ends(
    model: BoardModel, q: Pad, layer: str, obs: _Obstacles, *, width: float, clearance: float
) -> list[Point]:
    """R7 (ODH TPS63000: its 0.5 mm-pitch ground pins sat in pour islands that reached
    neither a via nor the exposed pad 0.3 mm away): stub ends that tie `q` to a larger pad
    of the same net and part on `layer` - an axis-parallel stub from `q`'s centre through
    the gap (at most `TIE_MAX_GAP_MM`) to `width` inside the other pad, clear of other-net
    copper. Offered after every via candidate (only a pad without a via spot uses it)."""
    part = next((p for p in model.parts if p.ref == q.ref), None)
    if part is None:
        return []
    qb = _bbox_of(_pad_rect(q))
    out: list[Point] = []
    for o in part.pads:
        if o is q or o.net != q.net or o.w * o.h <= q.w * q.h or not _pad_on_layer(o, layer):
            continue
        ob = _bbox_of(_pad_rect(o))
        for axis in (0, 1):
            other = 1 - axis
            if not ob[other] + width / 2 <= (q.x, q.y)[other] <= ob[other + 2] - width / 2:
                continue  # the straight stub would not land inside the other pad
            if qb[axis + 2] <= ob[axis]:
                gap, end_c = ob[axis] - qb[axis + 2], ob[axis] + width / 2
            elif ob[axis + 2] <= qb[axis]:
                gap, end_c = qb[axis] - ob[axis + 2], ob[axis + 2] - width / 2
            else:
                continue
            if gap > TIE_MAX_GAP_MM + 1e-9:
                continue
            end = [q.x, q.y]
            end[axis] = end_c
            at = (_snap(end[0]), _snap(end[1]))
            if obs.stub_ok((q.x, q.y), at, layer, half=width / 2, clearance=clearance, own=q):
                out.append(at)
    return sorted(set(out))


def pad_key(q: Pad, seen: Mapping[tuple[str, str], object]) -> tuple[str, str]:
    """(ref, number) of `q`; a repeated number of the same part (shield / exposed pads)
    gets "#2", "#3", ... in the order of `fanout_pads`."""
    key = (q.ref, q.number)
    n = 1
    while key in seen:
        n += 1
        key = (q.ref, f"{q.number}#{n}")
    return key


def _own_area(model: BoardModel, q: Pad) -> str | None:
    """The pad's own footprint when one of its rule areas holds the pad (e.g. a module's
    antenna keepout around its ground pads), so that area does not block the pad's own
    fanout (as `route.with_footprint_keepouts`)."""
    part = next((p for p in model.parts if p.ref == q.ref), None)
    if part is None:
        return None
    if any(len(ra) >= 3 and point_in_polygon((q.x, q.y), ra) for ra in part.rule_areas):
        return q.ref
    return None


# -- stitching ---------------------------------------------------------------------------


def _outline_poly(model: BoardModel, inset: float) -> list[Point]:
    """The rectangular outline inset by `inset` (corner radius shrunk accordingly)."""
    o = model.outline
    return rect_poly(
        o.x + inset,
        o.y + inset,
        o.w - 2 * inset,
        o.h - 2 * inset,
        max(o.corner_radius - inset, 0.0),
    )


def _ring_points(model: BoardModel, inset: float, pitch: float) -> list[Point]:
    """Points every `pitch` mm along the inset rounded rectangle (rect outlines only)."""
    o = model.outline
    if o.kind != "rect" or o.w <= 2 * inset or o.h <= 2 * inset:
        return []
    poly = _outline_poly(model, inset)
    edges = list(zip(poly, [*poly[1:], poly[0]], strict=True))
    total = sum(math.dist(a, b) for a, b in edges)
    n = max(int(total // pitch), 1)
    step = total / n
    out: list[Point] = []
    pos = 0.0
    k = 0
    acc = 0.0
    for a, b in edges:
        seg = math.dist(a, b)
        while k < n and pos <= acc + seg + 1e-12:
            t = 0.0 if seg <= 0 else (pos - acc) / seg
            out.append((_snap(a[0] + (b[0] - a[0]) * t), _snap(a[1] + (b[1] - a[1]) * t)))
            k += 1
            pos = k * step
        acc += seg
    return out


def edge_ring_points(model: BoardModel, *, via: tuple[float, float], pitch: float) -> list[Point]:
    """The raw edge-ring positions `stitch_candidates` uses (before any filter): every
    `pitch` mm along the outline inset by copper-to-edge + via radius + 0.5 mm."""
    return _ring_points(model, edge_clearance(model) + via[0] / 2 + RING_INSET_MM, pitch)


def _covered(model: BoardModel, net: str, at: Point) -> bool:
    """At least two zones of `net` on different copper layers cover `at` (zone
    outlines, not fills: a via elsewhere would dangle)."""
    layers: set[str] = set()
    for z in model.zones:
        if z.rule_area or z.net != net or len(z.outline) < 3:
            continue
        if point_in_polygon(at, z.outline):
            layers.update(z.layers)
            if len(layers) >= 2:
                return True
    return False


def _courtyard_hit(model: BoardModel, at: Point, r: float, bucket: _Bucket) -> bool:
    for i in bucket.near(at[0], at[1], at[0], at[1]):
        cy = model.parts[i].courtyard
        if point_in_polygon(at, cy) or _poly_dist(at, cy) < r:
            return True
    return False


def stitch_candidates(
    model: BoardModel,
    net: str,
    *,
    via: tuple[float, float],
    clearance: float,
    pitch: float = 5.0,
    edge_ring: bool = False,
) -> tuple[ViaCandidate, ...]:
    """Stitching vias of `net`: a `pitch` grid aligned to the outline origin + pitch/2,
    inside the outline inset by copper-to-edge + via radius + 0.25 mm; with `edge_ring`
    also points every `pitch` along the outline inset by copper-to-edge + via radius +
    0.5 mm (ring first). Same filters as the fanout plus: not in or touching a courtyard,
    covered by zones of `net` on at least two layers, spaced from each other
    (`vias_spaced`). Grid row by row (y, then x)."""
    o = model.outline
    if o.kind == "none" or not pitch > 0:
        return ()
    r = via[0] / 2
    edge = edge_clearance(model)
    h2h = hole_to_hole(model)
    obs = _Obstacles(model, net, reach=r + clearance + 1.0)
    courts = _Bucket()
    for i, p in enumerate(model.parts):
        if len(p.courtyard) >= 3:
            xs = [c[0] for c in p.courtyard]
            ys = [c[1] for c in p.courtyard]
            courts.add(i, min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r)
    raw: list[tuple[Point, float]] = []
    if edge_ring:
        inset = edge + r + RING_INSET_MM - 1e-6
        raw += [(pt, inset) for pt in edge_ring_points(model, via=via, pitch=pitch)]
    if o.kind == "rect":
        x0, y0, w, h = o.x, o.y, o.w, o.h
    else:
        xs = [p[0] for p in o.polygon]
        ys = [p[1] for p in o.polygon]
        x0, y0, w, h = min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
    nx = math.floor((w - pitch / 2) / pitch + 1e-9) + 1 if w >= pitch / 2 else 0
    ny = math.floor((h - pitch / 2) / pitch + 1e-9) + 1 if h >= pitch / 2 else 0
    grid_inset = edge + r + STITCH_INSET_MM
    for j in range(ny):
        for i in range(nx):
            raw.append(
                ((_snap(x0 + pitch / 2 + i * pitch), _snap(y0 + pitch / 2 + j * pitch)), grid_inset)
            )
    kept: list[Point] = []
    near = _Bucket()
    for at, inset in raw:
        if not _inside_outline(model, at, inset):
            continue
        if not _covered(model, net, at):
            continue
        if _courtyard_hit(model, at, r, courts):
            continue
        if not obs.via_ok(at, via=via, clearance=clearance, h2h=h2h):
            continue
        if any(
            not vias_spaced(at, kept[k], via=via, clearance=clearance, h2h=h2h)
            for k in near.near(at[0], at[1], at[0], at[1])
        ):
            continue
        reach = max(via[0] + clearance, via[1] + h2h)
        near.add(len(kept), at[0] - reach, at[1] - reach, at[0] + reach, at[1] + reach)
        kept.append(at)
    return tuple(ViaCandidate(at=at, stub=None, pad=None, rank=i) for i, at in enumerate(kept))
