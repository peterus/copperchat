"""Plain-Python 2D geometry for footprint checks (FP-C3, spec §8).

Units mm, y down, angles in degrees. A pad's rotation follows KiCad: a positive angle
turns counter-clockwise as seen on screen, which in y-down coordinates is
`x' = x*cos + y*sin`, `y' = -x*sin + y*cos`, applied around the pad centre.

Geometry the distance function understands:
- a polygon: `Sequence[Point]` (implicitly closed, at least 3 vertices),
- a `Seg`: a stroked segment (centre line `a`-`b` plus `half_width`),
- a circle: `("circle", centre, radius)`,
- a `Rounded`: a polygon core grown by a radius (a roundrect pad, exactly).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Literal, TypeIs

from copperchat.kicad_tools.footprints.model import Pad, Shape

Point = tuple[float, float]
Afford = Callable[[int], bool]  # charge n units of work; False = budget exhausted, stop
Box = tuple[float, float, float, float]  # x0, y0, x1, y1
Circle = tuple[Literal["circle"], Point, float]
Polygon = Sequence[Point]

ARC_STEPS_PER_QUARTER = 16
LOOP_TOL_MM = 0.001  # endpoints within 1 um are the same point


@dataclass(frozen=True)
class Seg:
    """A stroked segment: centre line a-b, `half_width` to each side."""

    a: Point
    b: Point
    half_width: float = 0.0


@dataclass(frozen=True)
class Rounded:
    """A filled polygon `core` grown by `radius` on every side (Minkowski sum with a
    disc): a rounded rectangle is its inner rectangle grown by the corner radius, so its
    distance is exact and costs 4 vertices instead of the 68 of its chord polygon."""

    core: tuple[Point, ...]
    radius: float


Part = Polygon | Seg | Circle  # what `pad_parts` and `stroke_segments` give
Geometry = Part | Rounded


def is_circle(g: object) -> TypeIs[Circle]:
    return isinstance(g, tuple) and len(g) == 3 and g[0] == "circle"


# --- building blocks ----------------------------------------------------------------


def _rotate(pts: Sequence[Point], center: Point, rotation: float) -> list[Point]:
    cx, cy = center
    if rotation % 360 == 0:
        return [(cx + x, cy + y) for x, y in pts]
    a = math.radians(rotation)
    c, s = math.cos(a), math.sin(a)
    return [(cx + x * c + y * s, cy - x * s + y * c) for x, y in pts]


def _dedupe(pts: list[Point]) -> list[Point]:
    out: list[Point] = []
    for p in pts:
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > 1e-9:
            out.append(p)
    if len(out) > 1 and math.hypot(out[0][0] - out[-1][0], out[0][1] - out[-1][1]) <= 1e-9:
        out.pop()
    return out


def _rounded_rect(w: float, h: float, r: float) -> list[Point]:
    """Rectangle centred on the origin with corner radius r (r=0: 4 vertices)."""
    hx, hy = w / 2, h / 2
    r = min(max(r, 0.0), hx, hy)
    if r <= 1e-9:
        return [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]
    out: list[Point] = []
    # corner centres in clockwise (on screen) order with the start angle of each arc
    for (cx, cy), start in (
        ((hx - r, -hy + r), -90.0),
        ((hx - r, hy - r), 0.0),
        ((-hx + r, hy - r), 90.0),
        ((-hx + r, -hy + r), 180.0),
    ):
        for i in range(ARC_STEPS_PER_QUARTER + 1):
            a = math.radians(start + 90.0 * i / ARC_STEPS_PER_QUARTER)
            out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return _dedupe(out)


def _chamfered_rect(w: float, h: float, c: float, corners: Sequence[str]) -> list[Point]:
    """Rectangle centred on the origin with the named corners cut by c (y down)."""
    hx, hy = w / 2, h / 2
    c = min(max(c, 0.0), hx, hy)
    out: list[Point] = []
    # clockwise on screen: top_left, top_right, bottom_right, bottom_left
    for name, (x, y), (ax, ay), (bx, by) in (
        ("top_left", (-hx, -hy), (0, 1), (1, 0)),
        ("top_right", (hx, -hy), (-1, 0), (0, 1)),
        ("bottom_right", (hx, hy), (0, -1), (-1, 0)),
        ("bottom_left", (-hx, hy), (1, 0), (0, -1)),
    ):
        if name in corners and c > 1e-9:
            out += [(x + ax * c, y + ay * c), (x + bx * c, y + by * c)]
        else:
            out.append((x, y))
    return out


def _trapezoid(w: float, h: float, delta: Point) -> list[Point]:
    """KiCad trapezoid: delta.x moves the left/right sides' corners on y (+-dx/2),
    delta.y the top/bottom sides' corners on x (+-dy/2). Order: bottom-left, top-left,
    top-right, bottom-right (y down)."""
    hx, hy = w / 2, h / 2
    dx, dy = delta[0] / 2, delta[1] / 2
    return [
        (-hx - dy, hy + dx),
        (-hx + dy, -hy - dx),
        (hx - dy, -hy + dx),
        (hx + dy, hy - dx),
    ]


def _translate_rotate(
    pts: Sequence[Point], local: Point, at: Point, rotation: float
) -> list[Point]:
    """Shift by `local` (pad frame), then rotate around the pad centre `at`."""
    return _rotate([(x + local[0], y + local[1]) for x, y in pts], at, rotation)


def _base_polygon(
    shape: str, size: Point, extra: dict[str, object], ratio: float | None
) -> list[Point]:
    w, h = size
    if shape == "roundrect":
        return _rounded_rect(w, h, (0.25 if ratio is None else ratio) * min(w, h))
    if shape == "oval":
        return _rounded_rect(w, h, min(w, h) / 2)
    if shape == "trapezoid":
        delta = extra.get("trapezoid_delta")
        if isinstance(delta, tuple | list) and len(delta) == 2:
            return _trapezoid(w, h, (float(delta[0]), float(delta[1])))
    if shape == "chamfered_rect":
        corners = extra.get("chamfered_corners")
        cr = extra.get("chamfer_ratio")
        if isinstance(corners, tuple | list) and isinstance(cr, int | float):
            return _chamfered_rect(w, h, float(cr) * min(w, h), [str(c) for c in corners])
    return _rounded_rect(w, h, 0.0)  # rect, unknown, or data missing


def board_size(pad: Pad) -> Point:
    """Pad size in board axes (swapped for a 90/270 degree pad)."""
    w, h = pad.size
    r = pad.rotation % 180
    return (h, w) if abs(r - 90) < 1e-6 else (w, h)


def pad_geometry(pad: Pad) -> Polygon | Circle:
    """Exact outline of the pad's main (anchor) shape: a `("circle", centre, r)` for
    round pads, else a polygon (arcs as 16 chords per quarter).

    rect, roundrect (ratio default 0.25), oval (stadium), trapezoid (`trapezoid_delta`)
    and chamfered_rect (`chamfer_ratio`, `chamfered_corners`) are exact; the copper
    `offset` is applied in the pad frame before the pad rotation (around the pad centre).
    For a custom pad this is only the anchor shape, use `pad_parts` for the whole pad.
    """
    ex: dict[str, object] = dict(pad.extra)
    shape = pad.shape
    if shape == "custom":
        shape = str(ex.get("anchor", "rect"))
    off = _vec_extra(ex.get("offset"))
    w, h = pad.size
    if shape == "circle":
        return (
            "circle",
            _translate_rotate([(0.0, 0.0)], off, pad.at, pad.rotation)[0],
            min(w, h) / 2,
        )
    base = _base_polygon(shape, pad.size, ex, pad.roundrect_ratio)
    return _translate_rotate(base, off, pad.at, pad.rotation)


def _vec_extra(v: object) -> Point:
    if isinstance(v, tuple | list) and len(v) == 2:
        return (float(v[0]), float(v[1]))
    return (0.0, 0.0)


def pad_parts(pad: Pad) -> list[Part]:
    """All copper pieces of a pad (rotated, absolute). One part for ordinary pads; a
    custom pad gives its anchor shape plus its primitives (filled polygon/rect as
    polygons, circle as a disc grown by half its stroke, segments/arcs as `Seg`s).
    Primitives sit at the shape position like the anchor shape (KiCad: pad position +
    rotated copper `offset`), so the offset applies to them too. Pad-to-pad gap is the
    minimum `distance` over the parts.
    """
    parts: list[Part] = [pad_geometry(pad)]
    if pad.shape != "custom":
        return parts
    extra = dict(pad.extra)
    off = _vec_extra(extra.get("offset"))
    prims = extra.get("primitives", ())
    for d in prims if isinstance(prims, tuple | list) else ():
        if not isinstance(d, dict):
            continue
        pts = [(float(x), float(y)) for x, y in d.get("points", [])]
        kind = d.get("kind")
        hw = float(d.get("width", 0.0)) / 2
        if kind == "polygon" and len(pts) >= 3:
            parts.append(_abs(pts, pad, off))
        elif kind == "rect" and len(pts) >= 2:
            (x0, y0), (x1, y1) = pts[0], pts[1]
            parts.append(_abs([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], pad, off))
        elif kind == "circle" and d.get("center") and d.get("size"):
            cx, cy = d["center"]
            c = _abs([(float(cx), float(cy))], pad, off)[0]
            parts.append(("circle", c, float(d["size"][0]) / 2 + hw))
        elif kind in ("segment", "arc") and len(pts) >= 2:
            chain = _arc_points(*pts[:3]) if kind == "arc" and len(pts) >= 3 else pts[:2]
            for sg in _chain(_abs(chain, pad, off), hw, False):
                parts.append(sg)
    return parts


def pad_part_counts(pad: Pad) -> tuple[int, int]:
    """(parts, vertices) of `pad_parts(pad)` without building them: the work a caller
    charges before it asks for them. Exact for a custom pad (its anchor is built, a
    handful of vertices; the primitives are counted); for any other pad an upper bound
    (one part: a chord polygon of at most 68 vertices, 8 for a chamfered rectangle, 4
    for any other, a circle 1)."""
    if pad.shape != "custom":
        if pad.shape == "circle":
            return 1, 1
        if pad.shape in ("roundrect", "oval"):
            return 1, 4 * (ARC_STEPS_PER_QUARTER + 1)
        return 1, 8 if pad.shape == "chamfered_rect" else 4
    main = pad_geometry(pad)
    parts, vertices = 1, 1 if is_circle(main) else len(main)
    prims = dict(pad.extra).get("primitives", ())
    for d in prims if isinstance(prims, tuple | list) else ():
        if not isinstance(d, dict):
            continue
        n = len(d.get("points", []))
        kind = d.get("kind")
        if kind == "polygon" and n >= 3:
            parts, vertices = parts + 1, vertices + n
        elif kind == "rect" and n >= 2:
            parts, vertices = parts + 1, vertices + 4
        elif kind == "circle" and d.get("center") and d.get("size"):
            parts, vertices = parts + 1, vertices + 1
        elif kind in ("segment", "arc") and n >= 2:
            k = 1
            if kind == "arc" and n >= 3:
                a, b, c = ((float(x), float(y)) for x, y in d["points"][:3])
                k = arc_chord_count(a, b, c)
            parts, vertices = parts + k, vertices + 2 * k
    return parts, vertices


def pad_distance_parts(pad: Pad, parts: Sequence[Part] | None = None) -> list[Geometry]:
    """`pad_parts` (or the given `parts`, its result) for distances: the main shape of a
    roundrect or oval pad is exact and small (a `Rounded` inner rectangle, a stadium
    `Seg`, or a circle) instead of its 68-vertex chord polygon; the other parts as is."""
    out: list[Geometry] = list(pad_parts(pad) if parts is None else parts)
    if pad.shape in ("roundrect", "oval"):
        out[0] = _rounded_main(pad)
    return out


def _rounded_main(pad: Pad) -> Geometry:
    w, h = pad.size
    hx, hy = w / 2, h / 2
    if pad.shape == "oval":
        r = min(hx, hy)
    else:
        ratio = 0.25 if pad.roundrect_ratio is None else pad.roundrect_ratio
        r = min(max(ratio * min(w, h), 0.0), hx, hy)
    off = _vec_extra(dict(pad.extra).get("offset"))
    ix, iy = hx - r, hy - r  # the inner rectangle's half sizes

    def place(pts: Sequence[Point]) -> list[Point]:
        return _translate_rotate(pts, off, pad.at, pad.rotation)

    if r <= 1e-9:
        return place(_rounded_rect(w, h, 0.0))
    if ix <= 1e-9 and iy <= 1e-9:
        return ("circle", place([(0.0, 0.0)])[0], r)
    if ix <= 1e-9 or iy <= 1e-9:
        a, b = place([(-ix, -iy), (ix, iy)])
        return Seg(a, b, r)
    return Rounded(tuple(place(_rounded_rect(2 * ix, 2 * iy, 0.0))), r)


def _abs(pts: Sequence[Point], pad: Pad, off: Point) -> list[Point]:
    return _translate_rotate(pts, off, pad.at, pad.rotation)


def pad_polygon(pad: Pad) -> list[Point]:
    """`pad_geometry` as a polygon; a round pad becomes 16 chords per quarter."""
    g = pad_geometry(pad)
    if is_circle(g):
        return _circle_points(g[1], g[2])
    return list(g)


def _circle_points(center: Point, r: float) -> list[Point]:
    n = 4 * ARC_STEPS_PER_QUARTER
    return [
        (
            center[0] + r * math.cos(2 * math.pi * i / n),
            center[1] + r * math.sin(2 * math.pi * i / n),
        )
        for i in range(n)
    ]


def _arc_sweep(start: Point, mid: Point, end: Point) -> tuple[Point, float, float, float] | None:
    """(centre, radius, start angle, signed sweep) of the arc start -> mid -> end; None
    when the three points are collinear."""
    ax, ay = start
    bx, by = mid
    cx, cy = end
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:  # collinear
        return None
    ux = (
        (ax**2 + ay**2) * (by - cy) + (bx**2 + by**2) * (cy - ay) + (cx**2 + cy**2) * (ay - by)
    ) / d
    uy = (
        (ax**2 + ay**2) * (cx - bx) + (bx**2 + by**2) * (ax - cx) + (cx**2 + cy**2) * (bx - ax)
    ) / d
    r = math.hypot(ax - ux, ay - uy)
    a0 = math.atan2(ay - uy, ax - ux)
    am = math.atan2(by - uy, bx - ux)
    a1 = math.atan2(cy - uy, cx - ux)
    two_pi = 2 * math.pi
    # sweep direction: the one that passes through the mid point
    ccw_mid = (am - a0) % two_pi
    ccw_end = (a1 - a0) % two_pi
    sweep = ccw_end if ccw_mid <= ccw_end else ccw_end - two_pi
    return (ux, uy), r, a0, sweep


def _arc_chords(sweep: float) -> int:
    return max(1, math.ceil(abs(sweep) / (math.pi / 2) * ARC_STEPS_PER_QUARTER))


def arc_chord_count(start: Point, mid: Point, end: Point) -> int:
    """Chords `_arc_points` gives for this arc, without building them."""
    arc = _arc_sweep(start, mid, end)
    return 1 if arc is None else _arc_chords(arc[3])


def _arc_points(start: Point, mid: Point, end: Point) -> list[Point]:
    """Chord points of the arc start -> mid -> end (both ends included)."""
    arc = _arc_sweep(start, mid, end)
    if arc is None:
        return [start, end]
    (ux, uy), r, a0, sweep = arc
    n = _arc_chords(sweep)
    pts = [
        (ux + r * math.cos(a0 + sweep * i / n), uy + r * math.sin(a0 + sweep * i / n))
        for i in range(n + 1)
    ]
    pts[0], pts[-1] = start, end
    return pts


def _chain(pts: Sequence[Point], half_width: float, closed: bool) -> list[Seg]:
    n = len(pts)
    last = n if closed else n - 1
    return [Seg(pts[i], pts[(i + 1) % n], half_width) for i in range(max(last, 0))]


def stroke_segments(shape: Shape) -> list[Seg]:
    """Centre lines of a graphic as segments carrying half the stroke width.

    Arcs and circles become chords (16 per quarter). `rect` gives its 4 sides (corner
    radius ignored), `polygon`/`polygon_hole` a closed ring (arc nodes inside a polygon
    are taken as plain vertices, as the model flattens them), `ellipse` 64 chords,
    `bezier` its control polygon. Unsupported kinds (`ellipse_arc`) give [].
    """
    hw = shape.width / 2
    k = shape.kind
    if k == "segment" and len(shape.points) >= 2:
        return [Seg(shape.points[0], shape.points[1], hw)]
    if k == "rect" and len(shape.points) >= 2:
        (x0, y0), (x1, y1) = shape.points[0], shape.points[1]
        return _chain([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], hw, True)
    if k == "arc" and len(shape.points) >= 3:
        return _chain(_arc_points(*shape.points[:3]), hw, False)
    if k == "circle" and shape.center is not None and shape.size is not None:
        return _chain(_circle_points(shape.center, shape.size[0] / 2), hw, True)
    if k in ("polygon", "polygon_hole"):
        return _chain(shape.points, hw, shape.closed or len(shape.points) > 2)
    if k == "bezier":
        return _chain(shape.points, hw, False)
    if k == "ellipse" and shape.center is not None and shape.size is not None:
        n = 4 * ARC_STEPS_PER_QUARTER
        rx, ry = shape.size[0] / 2, shape.size[1] / 2
        unit = [
            (rx * math.cos(2 * math.pi * i / n), ry * math.sin(2 * math.pi * i / n))
            for i in range(n)
        ]
        return _chain(_rotate(unit, shape.center, shape.rotation or 0.0), hw, True)
    return []


def segment_count(shape: Shape) -> int:
    """`len(stroke_segments(shape))` without building them: the tessellation work a
    caller charges before it asks for the segments."""
    k, n = shape.kind, len(shape.points)
    if k == "segment":
        return 1 if n >= 2 else 0
    if k == "rect":
        return 4 if n >= 2 else 0
    if k == "arc":
        return arc_chord_count(*shape.points[:3]) if n >= 3 else 0
    if k in ("circle", "ellipse"):
        ok = shape.center is not None and shape.size is not None
        return 4 * ARC_STEPS_PER_QUARTER if ok else 0
    if k in ("polygon", "polygon_hole"):
        return n if shape.closed or n > 2 else max(n - 1, 0)
    if k == "bezier":
        return max(n - 1, 0)
    return 0


# --- distance -----------------------------------------------------------------------


def point_in_polygon(p: Point, poly: Polygon) -> bool:
    """Even-odd test; a point on the boundary counts as inside."""
    x, y = p
    n = len(poly)
    inside = False
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if _point_seg_dist(p, (x0, y0), (x1, y1)) <= 1e-12:
            return True
        if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / (y1 - y0) + x0:
            inside = not inside
    return inside


class BandIndex:
    """`point_in_polygon` with the polygon's edges bucketed into horizontal bands: a
    point is tested against the edges of its band only (the crossings and the boundary
    test only ever involve edges whose y-range holds the point's y), outside the
    polygon's box against none. `cost` is the most edges of one band: the work units of
    one `contains`. Build with `BandIndex.build_cost` charged first (an edge enters
    every band its y-range spans)."""

    _PAD = 1e-9  # an edge's y-range, grown so a boundary point (1e-12) finds its edge

    def __init__(self, poly: Polygon) -> None:
        self.poly = poly
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        self.box = (min(xs), min(ys), max(xs), max(ys))
        n = len(poly)
        self.n = max(n, 1)
        span = self.box[3] - self.box[1]
        self.h = span / self.n if span > 0 else 1.0
        self.bands: list[list[tuple[Point, Point]]] = [[] for _ in range(self.n)]
        for i in range(n):
            a, b = poly[i], poly[(i + 1) % n]
            lo, hi = self._range(a, b)
            for k in range(lo, hi + 1):
                self.bands[k].append((a, b))
        self.cost = max((len(b) for b in self.bands), default=0) + 1

    def _band(self, y: float) -> int:
        return min(self.n - 1, max(0, int((y - self.box[1]) / self.h)))

    def _range(self, a: Point, b: Point) -> tuple[int, int]:
        lo = self._band(min(a[1], b[1]) - self._PAD)
        return lo, self._band(max(a[1], b[1]) + self._PAD)

    @staticmethod
    def build_cost(poly: Polygon) -> int:
        """Work of building the index (edge-band entries), without building it."""
        n = len(poly)
        if n == 0:
            return 1
        y0 = min(p[1] for p in poly)
        span = max(p[1] for p in poly) - y0
        h = span / n if span > 0 else 1.0
        total = n
        for i in range(n):
            a, b = poly[i], poly[(i + 1) % n]
            lo = min(n - 1, max(0, int((min(a[1], b[1]) - BandIndex._PAD - y0) / h)))
            hi = min(n - 1, max(0, int((max(a[1], b[1]) + BandIndex._PAD - y0) / h)))
            total += hi - lo + 1
        return total

    def contains(self, p: Point) -> bool:
        """As `point_in_polygon` (even-odd, boundary = inside)."""
        x, y = p
        x0, y0, x1, y1 = self.box
        if not (x0 - 1e-12 <= x <= x1 + 1e-12 and y0 - 1e-12 <= y <= y1 + 1e-12):
            return False
        inside = False
        for (ax, ay), (bx, by) in self.bands[self._band(y)]:
            if _point_seg_dist(p, (ax, ay), (bx, by)) <= 1e-12:
                return True
            if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
                inside = not inside
        return inside


def _point_seg_dist(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _cross(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _seg_seg_dist(a: Point, b: Point, c: Point, d: Point) -> float:
    d1, d2 = _cross(a, b, c), _cross(a, b, d)
    d3, d4 = _cross(c, d, a), _cross(c, d, b)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and 0 not in (d1, d2, d3, d4):
        return 0.0
    return min(
        _point_seg_dist(a, c, d),
        _point_seg_dist(b, c, d),
        _point_seg_dist(c, a, b),
        _point_seg_dist(d, a, b),
    )


def _core(g: Geometry) -> tuple[list[Point], bool, float]:
    """(vertices, is_polygon, inflate radius) of any supported geometry."""
    if isinstance(g, Seg):
        return [g.a, g.b], False, g.half_width
    if is_circle(g):
        return [g[1]], False, g[2]
    if isinstance(g, Rounded):
        return list(g.core), len(g.core) >= 3, g.radius
    return list(g), len(g) >= 3, 0.0


def _core_distance(pa: list[Point], poly_a: bool, pb: list[Point], poly_b: bool) -> float:
    if poly_a and point_in_polygon(pb[0], pa):
        return 0.0
    if poly_b and point_in_polygon(pa[0], pb):
        return 0.0
    ea = [(pa[i], pa[(i + 1) % len(pa)]) for i in range(len(pa))] if poly_a else [(pa[0], pa[-1])]
    eb = [(pb[i], pb[(i + 1) % len(pb)]) for i in range(len(pb))] if poly_b else [(pb[0], pb[-1])]
    return min(_seg_seg_dist(a0, a1, b0, b1) for a0, a1 in ea for b0, b1 in eb)


def distance(a: Geometry, b: Geometry) -> float:
    """Shortest gap between two shapes; 0.0 when they touch, overlap or nest.

    Polygons are filled regions (containment gives 0); a `Seg` is a stadium (centre line
    inflated by its half width); a circle is a filled disc; a `Rounded` its core polygon
    inflated by its radius.
    """
    pa, poly_a, ra = _core(a)
    pb, poly_b, rb = _core(b)
    return max(0.0, _core_distance(pa, poly_a, pb, poly_b) - ra - rb)


def is_convex(poly: Polygon) -> bool:
    """True when every turn of the closed polygon goes the same way (collinear vertices
    allowed): then a region whose points are all inside it lies inside it."""
    n = len(poly)
    sign = 0
    for i in range(n):
        c = _cross(poly[i], poly[(i + 1) % n], poly[(i + 2) % n])
        if abs(c) <= 1e-12:
            continue
        if sign == 0:
            sign = 1 if c > 0 else -1
        elif (c > 0) != (sign > 0):
            return False
    return True


def segments_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    """True when a-b and c-d properly cross (each strictly separates the other's ends;
    touching or collinear is not crossing)."""
    d1, d2 = _cross(a, b, c), _cross(a, b, d)
    d3, d4 = _cross(c, d, a), _cross(c, d, b)
    return d1 * d2 < 0 and d3 * d4 < 0


def crossing_point(a: Point, b: Point, c: Point, d: Point) -> Point:
    """Where the lines a-b and c-d meet (for segments that `segments_cross`)."""
    d1, d2 = _cross(c, d, a), _cross(c, d, b)
    t = d1 / (d1 - d2)
    return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))


# --- outlines and pair search -------------------------------------------------------


def closed_outline(shapes: Sequence[Shape], afford: Afford | None = None) -> list[Point] | None:
    """One closed polygon from graphics: a rect, a polygon, a circle, or segments/arcs
    whose endpoints chain into a single loop within 1 um. None otherwise, also when
    `afford` refuses the work of an endpoint lookup (many endpoints in one cell)."""
    if not shapes:
        return None
    if len(shapes) == 1:
        s = shapes[0]
        if s.kind == "rect" and len(s.points) >= 2:
            (x0, y0), (x1, y1) = s.points[0], s.points[1]
            return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        if s.kind == "polygon" and len(s.points) >= 3:
            return list(s.points)
        if s.kind == "circle" and s.center is not None and s.size is not None:
            return _circle_points(s.center, s.size[0] / 2)
    if any(s.kind not in ("segment", "arc") for s in shapes):
        return None
    if afford is not None and not afford(sum(segment_count(s) + 1 for s in shapes)):
        return None  # the tessellation of the arcs, charged before it is built
    pieces: list[list[Point]] = []
    for s in shapes:
        if s.kind == "segment" and len(s.points) >= 2:
            pieces.append([s.points[0], s.points[1]])
        elif s.kind == "arc" and len(s.points) >= 3:
            pieces.append(_arc_points(*s.points[:3]))
        else:
            return None

    def near(p: Point, q: Point) -> bool:
        return math.hypot(p[0] - q[0], p[1] - q[1]) <= LOOP_TOL_MM

    # endpoint index on a LOOP_TOL_MM grid: two points within the tolerance lie in the
    # same or a neighbouring cell, so a lookup checks the 3 x 3 cells around a point.
    # The match is the lowest-index unused piece (start before end), as a linear scan.
    def cell(p: Point) -> tuple[int, int]:
        return (math.floor(p[0] / LOOP_TOL_MM), math.floor(p[1] / LOOP_TOL_MM))

    index: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for i, piece in enumerate(pieces):
        if i == 0:
            continue
        for end, p in ((0, piece[0]), (1, piece[-1])):
            index.setdefault(cell(p), []).append((i, end))
    used = [False] * len(pieces)
    used[0] = True
    path = list(pieces[0])
    for _ in range(len(pieces) - 1):
        tail = path[-1]
        cx, cy = cell(tail)
        best: tuple[int, int] | None = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                found = index.get((cx + dx, cy + dy), ())
                if afford is not None and not afford(len(found) + 1):
                    return None
                for i, end in found:
                    p = pieces[i][-1] if end else pieces[i][0]
                    if not used[i] and (best is None or (i, end) < best) and near(tail, p):
                        best = (i, end)
        if best is None:
            return None
        i, end = best
        used[i] = True
        path += pieces[i][-2::-1] if end else pieces[i][1:]
    if len(path) < 4 or not near(path[0], path[-1]):
        return None
    return path[:-1]


def candidate_pairs(
    boxes: Sequence[Box], gap: float, afford: Afford | None = None
) -> Iterator[tuple[int, int]]:
    """Index pairs (i < j) whose boxes are within `gap` of each other on both axes.

    Boxes are (x0, y0, x1, y1). Sort-and-sweep on x with an active list, then a y
    overlap test; boxes that are farther apart than `gap` on either axis are never
    yielded. With `afford`, each box first charges the active boxes it is tested
    against; when that is refused the sweep stops (the caller's budget records it).
    """
    order = sorted(range(len(boxes)), key=lambda i: boxes[i][0])
    active: list[int] = []
    for i in order:
        box = boxes[i]
        if afford is not None and not afford(len(active) + 1):
            return
        keep: list[int] = []
        for j in active:
            other = boxes[j]
            if other[2] + gap < box[0]:
                continue  # ends left of every later box: dropped for good
            keep.append(j)
            if other[1] <= box[3] + gap and box[1] <= other[3] + gap:
                yield (i, j) if i < j else (j, i)
        keep.append(i)
        active = keep


def cross_pairs(
    a: Sequence[Box], b: Sequence[Box], gap: float, afford: Afford | None = None
) -> Iterator[tuple[int, int]]:
    """Index pairs (i into `a`, j into `b`) whose boxes are within `gap` on both axes.

    A sweep on x over both groups that only ever pairs across them: unlike
    `candidate_pairs` on the concatenation it never visits the (possibly quadratic)
    pairs inside one group, e.g. thousands of overlapping pieces of one custom pad.
    `afford` as in `candidate_pairs`.
    """
    groups = (a, b)
    events = sorted(
        [(box[0], 0, i) for i, box in enumerate(a)] + [(box[0], 1, j) for j, box in enumerate(b)]
    )
    active: tuple[list[int], list[int]] = ([], [])
    for x0, g, k in events:
        box, other = groups[g][k], 1 - g
        if afford is not None and not afford(len(active[other]) + 1):
            return
        keep: list[int] = []
        for m in active[other]:
            ob = groups[other][m]
            if ob[2] + gap < x0:
                continue  # ends left of every later box: dropped for good
            keep.append(m)
            if ob[1] <= box[3] + gap and box[1] <= ob[3] + gap:
                yield (k, m) if g == 0 else (m, k)
        active[other][:] = keep
        active[g].append(k)
