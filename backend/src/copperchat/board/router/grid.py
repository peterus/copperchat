"""Obstacle grid of the octilinear router (B-C12, P5a Task 1).

The grid is a lattice of track-centre positions (`pitch` mm apart) over a window around one
connection. A node is blocked (2) when a track centre there would come closer than the
required distance to other-net copper, a `tracks` keepout or the board edge; own-net copper
is 1, everything else 0. `via_blocked` marks the nodes where a via of the request's
diameter would. Marking is done row by row with exact intervals, so a node is blocked
exactly when its distance is below the requirement plus `chord_slack(...)` — the most a
straight step between two free nodes can dip towards a rounded obstacle. The obstacle
shapes are kept as well (`shapes`), so a finished path can be checked exactly
(`path_clear`).

The lattice is aligned to the request's first pad centre (`req.a`), so a path starts
exactly at it; only the far end needs the off-grid snap (`simplify.snap_ends`).
"""

from __future__ import annotations

import itertools
import math
import re
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from copperchat.board.geom import _Grid, _pad_on_layer, convex_hull, point_in_polygon
from copperchat.board.model import ArcTrack, BoardModel, GraphicItem, Pad, Point

WINDOW_MM = 6.0
MAX_CELLS_PER_LAYER = 4_000_000
EDGE_MIN_MM = 0.3  # board edge clearance floor (spec §10.2)
MAX_SIZE_MM = 100.0  # sizes are bounded like every tool input (plan call 6)

FREE, OWN, BLOCKED = 0, 1, 2

_EPS = 1e-9


@dataclass(frozen=True)
class RouteRequest:
    net: str
    a: Pad
    b: Pad
    width: float
    clearance: float
    layers: tuple[str, ...]
    allow_vias: bool
    via: tuple[float, float]  # (diameter, drill)


@dataclass(frozen=True)
class RoutePath:
    segments: tuple[tuple[str, Point, Point], ...]  # (layer, start, end)
    vias: tuple[Point, ...]
    length: float


class DeadlineExpired(Exception):
    """A routing deadline passed while building a grid or checking reachability; the
    entry points (`solve`, `route_pair`, `solve_pair`, `route`) turn it into None."""


def check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise DeadlineExpired


class RouteTooLarge(Exception):
    """The routing window would exceed `MAX_CELLS_PER_LAYER`; nothing was allocated."""

    def __init__(self, cells: int) -> None:
        super().__init__(f"routing window of {cells} cells per layer exceeds the cap")
        self.cells = cells

    def __reduce__(self) -> tuple[type[RouteTooLarge], tuple[int]]:
        return (RouteTooLarge, (self.cells,))


# --- exact geometry ------------------------------------------------------------------------

# Obstacle cores. Each carries `need`: the smallest allowed distance from a track centre
# line to the core.
#   ("d", cx, cy, radius, need)          disc
#   ("s", ax, ay, bx, by, need)          segment (zero thickness; widths are in `need`)
#   ("r", x0, y0, x1, y1, need)          axis-parallel rectangle
#   ("p", points, need)                  filled polygon
Shape = tuple[Any, ...]


def _pt_seg(p: Point, a: Point, b: Point) -> float:
    vx, vy = b[0] - a[0], b[1] - a[1]
    ll = vx * vx + vy * vy
    if ll <= 0.0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / ll))
    return math.hypot(p[0] - a[0] - t * vx, p[1] - a[1] - t * vy)


def _orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segs_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    d1, d2 = _orient(c, d, a), _orient(c, d, b)
    d3, d4 = _orient(a, b, c), _orient(a, b, d)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 * d2 < 0 and d3 * d4 < 0


def seg_seg(a: Point, b: Point, c: Point, d: Point) -> float:
    """Distance between segments ab and cd."""
    if _segs_cross(a, b, c, d):
        return 0.0
    return min(_pt_seg(a, c, d), _pt_seg(b, c, d), _pt_seg(c, a, b), _pt_seg(d, a, b))


def _in_rect(p: Point, r: Shape) -> bool:
    return bool(r[1] <= p[0] <= r[3] and r[2] <= p[1] <= r[4])


def _edges(pts: Sequence[Point]) -> Iterator[tuple[Point, Point]]:
    n = len(pts)
    for k in range(n):
        yield pts[k], pts[(k + 1) % n]


def seg_shape(p: Point, q: Point, s: Shape) -> float:
    """Distance from segment pq to the core of shape `s` (0 when they overlap)."""
    kind = s[0]
    if kind == "d":
        return max(0.0, _pt_seg((s[1], s[2]), p, q) - float(s[3]))
    if kind == "s":
        return seg_seg(p, q, (s[1], s[2]), (s[3], s[4]))
    if kind == "r":
        if _in_rect(p, s) or _in_rect(q, s):
            return 0.0
        x0, y0, x1, y1 = s[1], s[2], s[3], s[4]
        corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        return min(seg_seg(p, q, c, d) for c, d in _edges(corners))
    pts = s[1]
    if point_in_polygon(p, pts) or point_in_polygon(q, pts):
        return 0.0
    return min(seg_seg(p, q, c, d) for c, d in _edges(pts))


def _shape_bbox(s: Shape) -> tuple[float, float, float, float]:
    kind = s[0]
    if kind == "d":
        r = s[3]
        return (s[1] - r, s[2] - r, s[1] + r, s[2] + r)
    if kind in ("s", "r"):
        return (min(s[1], s[3]), min(s[2], s[4]), max(s[1], s[3]), max(s[2], s[4]))
    xs = [p[0] for p in s[1]]
    ys = [p[1] for p in s[1]]
    return (min(xs), min(ys), max(xs), max(ys))


def _shape_need(s: Shape) -> float:
    return float(s[-1])


def chord_slack(need: float, pitch: float) -> float:
    """The deepest a straight step (≤ pitch·√2) between two nodes whose distance to a core
    is ≥ `need` can come to that core (sagitta; the region within `need` of a convex core
    has boundary curvature radius ≥ `need`)."""
    half = pitch * math.sqrt(2.0) / 2.0
    if need <= half:
        return need
    return need - math.sqrt(need * need - half * half) + 1e-9


# --- row intervals ------------------------------------------------------------------------


def _linear(c1: float, c0: float, lo: float, hi: float) -> tuple[float, float] | None:
    """x-interval where lo <= c1*x + c0 <= hi (None when empty)."""
    if abs(c1) < 1e-15:
        return (-math.inf, math.inf) if lo <= c0 <= hi else None
    x1, x2 = (lo - c0) / c1, (hi - c0) / c1
    return (min(x1, x2), max(x1, x2))


def _cap(
    a: tuple[float, float] | None, b: tuple[float, float] | None
) -> tuple[float, float] | None:
    if a is None or b is None:
        return None
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    return (lo, hi) if lo <= hi else None


def _disc_row(cx: float, cy: float, r: float, y: float) -> tuple[float, float] | None:
    dy = y - cy
    if abs(dy) > r:
        return None
    half = math.sqrt(max(r * r - dy * dy, 0.0))
    return (cx - half, cx + half)


def _hull(parts: Iterable[tuple[float, float] | None]) -> tuple[float, float] | None:
    got = [p for p in parts if p is not None]
    if not got:
        return None
    return (min(p[0] for p in got), max(p[1] for p in got))


def _capsule_row(
    ax: float, ay: float, bx: float, by: float, t: float, y: float
) -> tuple[float, float] | None:
    """Row interval of {p : dist(p, segment ab) <= t} (convex, so one interval)."""
    vx, vy = bx - ax, by - ay
    length = math.hypot(vx, vy)
    if length < 1e-12:
        return _disc_row(ax, ay, t, y)
    ux, uy = vx / length, vy / length
    nx, ny = -uy, ux
    band = _cap(
        _linear(nx, ny * (y - ay) - nx * ax, -t, t),
        _linear(ux, uy * (y - ay) - ux * ax, 0.0, length),
    )
    return _hull((band, _disc_row(ax, ay, t, y), _disc_row(bx, by, t, y)))


def _rect_row(
    x0: float, y0: float, x1: float, y1: float, t: float, y: float
) -> tuple[float, float] | None:
    if y0 <= y <= y1:
        return (x0 - t, x1 + t)
    dy = y0 - y if y < y0 else y - y1
    if dy > t:
        return None
    half = math.sqrt(max(t * t - dy * dy, 0.0))
    return (x0 - half, x1 + half)


def _poly_row(pts: Sequence[Point], y: float) -> list[tuple[float, float]]:
    """Inside intervals of a polygon on row y (even-odd)."""
    xs: list[float] = []
    for (ax, ay), (bx, by) in _edges(pts):
        if (ay <= y < by) or (by <= y < ay):
            xs.append(ax + (y - ay) * (bx - ax) / (by - ay))
    xs.sort()
    return [(xs[k], xs[k + 1]) for k in range(0, len(xs) - 1, 2)]


# --- the grid -----------------------------------------------------------------------------


@dataclass(eq=False)
class ObstacleGrid:
    pitch: float
    x0: float
    y0: float
    w: int
    h: int
    layers: tuple[str, ...]
    cells: tuple[bytearray, ...]
    via_blocked: bytearray
    # exact obstacle cores per layer (index-aligned with `layers`), each with its `need`
    shapes: tuple[tuple[Shape, ...], ...] = ()
    outline: tuple[Point, ...] = ()  # board outline polygon (() = none)
    inset: float = 0.0  # required track-centre distance from the outline
    # optional per-layer preference band (pair fallback): 1 = cost x PAIR_BAND_FACTOR
    cheap: tuple[bytearray, ...] = ()
    # exact via obstacles (need = distance from a via centre) and the via's edge inset
    via_shapes: tuple[Shape, ...] = ()
    via_inset: float = 0.0
    _index: list[_Grid] | None = field(default=None, repr=False)

    def cell(self, x: float, y: float) -> tuple[int, int]:
        return (round((x - self.x0) / self.pitch), round((y - self.y0) / self.pitch))

    def xy(self, i: int, j: int) -> Point:
        return (self.x0 + i * self.pitch, self.y0 + j * self.pitch)

    def inside(self, i: int, j: int) -> bool:
        return 0 <= i < self.w and 0 <= j < self.h

    def free(self, li: int, i: int, j: int) -> bool:
        return self.inside(i, j) and self.cells[li][j * self.w + i] != BLOCKED

    def via_ok(self, i: int, j: int) -> bool:
        return self.inside(i, j) and not self.via_blocked[j * self.w + i]

    # -- exact check ------------------------------------------------------------------------

    def _shape_index(self) -> list[_Grid]:
        if self._index is None:
            index: list[_Grid] = []
            for shapes in self.shapes:
                g = _Grid(cell=2.0)
                # clipped to the window: a huge item only fills the buckets it can reach
                wx0, wy0 = self.x0 - 1.0, self.y0 - 1.0
                wx1, wy1 = self.x0 + self.w * self.pitch + 1.0, self.y0 + self.h * self.pitch + 1.0
                for k, s in enumerate(shapes):
                    x0, y0, x1, y1 = _shape_bbox(s)
                    n = _shape_need(s)
                    box = (max(x0 - n, wx0), max(y0 - n, wy0), min(x1 + n, wx1), min(y1 + n, wy1))
                    if box[0] <= box[2] and box[1] <= box[3]:
                        g.add(k, box)
                index.append(g)
            self._index = index
        return self._index

    def segment_clear(self, li: int, p: Point, q: Point, *, tol: float = 1e-7) -> bool:
        """Segment pq (a track centre line on layer `li`) keeps every obstacle's `need` and
        stays inside the outline inset."""
        box = (min(p[0], q[0]), min(p[1], q[1]), max(p[0], q[0]), max(p[1], q[1]))
        shapes = self.shapes[li] if li < len(self.shapes) else ()
        if shapes:
            for k in self._shape_index()[li].query(box):
                s = shapes[k]
                if seg_shape(p, q, s) < _shape_need(s) - tol:
                    return False
        if self.outline:
            if not (point_in_polygon(p, self.outline) and point_in_polygon(q, self.outline)):
                return False
            for c, d in _edges(self.outline):
                if seg_seg(p, q, c, d) < self.inset - tol:
                    return False
        return True


def via_clear(grid: ObstacleGrid, at: Point, *, tol: float = 1e-7) -> bool:
    """A via at `at` keeps every via obstacle's need and the outline's via inset."""
    for s in grid.via_shapes:
        if seg_shape(at, at, s) < _shape_need(s) - tol:
            return False
    if grid.outline:
        if not point_in_polygon(at, grid.outline):
            return False
        return all(_pt_seg(at, c, d) >= grid.via_inset - tol for c, d in _edges(grid.outline))
    return True


def path_clear(grid: ObstacleGrid, path: RoutePath) -> bool:
    """Every segment of `path` passes `grid.segment_clear` on its layer and every via
    `via_clear`."""
    li_of = {name: k for k, name in enumerate(grid.layers)}
    if any(layer not in li_of for layer, _p, _q in path.segments):
        return False
    if not all(grid.segment_clear(li_of[layer], p, q) for layer, p, q in path.segments):
        return False
    return all(via_clear(grid, v) for v in path.vias)


def _rule(model: BoardModel, name: str, default: float) -> float:
    for key, value in model.rules:
        if key == name:
            return float(value)
    return default


def edge_clearance(model: BoardModel) -> float:
    return max(_rule(model, "copper_edge_clearance", 0.0), EDGE_MIN_MM)


def window_bounds(
    model: BoardModel, reqs: Sequence[RouteRequest], window_mm: float
) -> tuple[float, float, float, float]:
    """The search window: bbox of the requests' pad centres grown by `window_mm`, clipped to
    the outline bbox (when there is an outline)."""
    xs = [p.x for r in reqs for p in (r.a, r.b)]
    ys = [p.y for r in reqs for p in (r.a, r.b)]
    x0, y0 = min(xs) - window_mm, min(ys) - window_mm
    x1, y1 = max(xs) + window_mm, max(ys) + window_mm
    poly = model.outline.polygon if model.outline.kind != "none" else ()
    if poly:
        ox0, oy0 = min(p[0] for p in poly), min(p[1] for p in poly)
        ox1, oy1 = max(p[0] for p in poly), max(p[1] for p in poly)
        x0, y0, x1, y1 = max(x0, ox0), max(y0, oy0), min(x1, ox1), min(y1, oy1)
    return x0, y0, x1, y1


def _pad_shape(p: Pad, need: float) -> Shape:
    rot = p.rot % 90.0
    if p.shape == "circle":
        return ("d", p.x, p.y, max(p.w, p.h) / 2, need)
    if min(rot, 90.0 - rot) <= 1e-6:
        w, h = (p.h, p.w) if round(p.rot / 90.0) % 2 else (p.w, p.h)
        return ("r", p.x - w / 2, p.y - h / 2, p.x + w / 2, p.y + h / 2, need)
    return ("d", p.x, p.y, math.hypot(p.w, p.h) / 2, need)


def _arc_points(a: ArcTrack) -> tuple[list[Point], float]:
    """Chord points along an arc track and the chords' sagitta (added to the need)."""
    (x1, y1), (x2, y2), (x3, y3) = a.start, a.mid, a.end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [a.start, a.end], 0.0
    ux = (
        (x1**2 + y1**2) * (y2 - y3) + (x2**2 + y2**2) * (y3 - y1) + (x3**2 + y3**2) * (y1 - y2)
    ) / d
    uy = (
        (x1**2 + y1**2) * (x3 - x2) + (x2**2 + y2**2) * (x1 - x3) + (x3**2 + y3**2) * (x2 - x1)
    ) / d
    r = math.hypot(x1 - ux, y1 - uy)
    t1 = math.atan2(y1 - uy, x1 - ux)
    t2 = math.atan2(y2 - uy, x2 - ux)
    t3 = math.atan2(y3 - uy, x3 - ux)
    span = (t3 - t1) % (2 * math.pi)
    if (t2 - t1) % (2 * math.pi) > span:  # the mid point is on the other way round
        span -= 2 * math.pi
    n = max(2, math.ceil(abs(span) / 0.1))
    pts = [
        (ux + r * math.cos(t1 + span * k / n), uy + r * math.sin(t1 + span * k / n))
        for k in range(n + 1)
    ]
    sag = r * (1 - math.cos(abs(span) / n / 2))
    return pts, sag


def _graphic_shapes(kind: str, pts: Sequence[Point], need: float) -> list[Shape]:
    """Copper graphics: strokes as segments; closed shapes are treated as filled."""
    if kind == "segment" and len(pts) == 2:
        return [("s", *pts[0], *pts[1], need)]
    if kind == "arc" and len(pts) == 3:
        chords, sag = _arc_points(ArcTrack("", "", "", pts[0], pts[1], pts[2], 0.0, False))
        return [("s", *p, *q, need + sag) for p, q in itertools.pairwise(chords)]
    if kind == "circle" and len(pts) == 2:
        return [("d", *pts[0], math.dist(pts[0], pts[1]), need)]
    if kind == "rect" and len(pts) == 2:
        (x0, y0), (x1, y1) = pts
        return [("r", min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), need)]
    if kind in ("polygon", "bezier") and len(pts) >= 3:
        if kind == "bezier":  # the curve lies in its control points' hull
            pts = convex_hull(list(pts))
        return [("p", tuple(pts), need)]
    if len(pts) >= 2:
        return [("s", *p, *q, need) for p, q in itertools.pairwise(pts)]
    return []


def _layers_of(names: Iterable[str], copper: Sequence[str]) -> set[str]:
    out: set[str] = set()
    for n in names:
        if n == "*.Cu":
            out.update(copper)
        else:
            out.add(n)
    return out


class _Marker:
    """Row-interval marking of one window (all layers share the lattice)."""

    def __init__(self, x0: float, y0: float, w: int, h: int, pitch: float) -> None:
        self.x0, self.y0, self.w, self.h, self.p = x0, y0, w, h, pitch

    def rows(self, ylo: float, yhi: float) -> range:
        j0 = max(0, math.ceil((ylo - self.y0) / self.p - 1e-9))
        j1 = min(self.h - 1, math.floor((yhi - self.y0) / self.p + 1e-9))
        return range(j0, j1 + 1)

    def fill(self, buf: bytearray, j: int, xl: float, xr: float, value: int) -> None:
        lo = (xl - self.x0) / self.p - 1e-9
        hi = (xr - self.x0) / self.p + 1e-9
        i0 = 0 if lo <= 0 else math.ceil(lo) if lo < self.w else self.w
        i1 = self.w - 1 if hi >= self.w - 1 else math.floor(hi) if hi >= 0 else -1
        if i1 >= i0:
            base = j * self.w
            buf[base + i0 : base + i1 + 1] = bytes([value]) * (i1 - i0 + 1)

    def y(self, j: int) -> float:
        return self.y0 + j * self.p

    def shape(self, buf: bytearray, s: Shape, t: float, value: int) -> None:
        """Mark every node within `t` of the core of `s`."""
        kind = s[0]
        _x0, y0, _x1, y1 = _shape_bbox(s)
        for j in self.rows(y0 - t, y1 + t):
            y = self.y(j)
            if kind == "d":
                iv = _disc_row(s[1], s[2], s[3] + t, y)
            elif kind == "s":
                iv = _capsule_row(s[1], s[2], s[3], s[4], t, y)
            elif kind == "r":
                iv = _rect_row(s[1], s[2], s[3], s[4], t, y)
            else:
                for lo, hi in _poly_row(s[1], y):
                    self.fill(buf, j, lo, hi, value)
                iv = None
            if iv is not None:
                self.fill(buf, j, iv[0], iv[1], value)
        if kind == "p":
            for (ax, ay), (bx, by) in _edges(s[1]):
                for j in self.rows(min(ay, by) - t, max(ay, by) + t):
                    iv = _capsule_row(ax, ay, bx, by, t, self.y(j))
                    if iv is not None:
                        self.fill(buf, j, iv[0], iv[1], value)

    def outside(self, buf: bytearray, poly: Sequence[Point], t: float, value: int) -> None:
        """Mark every node outside `poly` or within `t` of its boundary."""
        for j in range(self.h):
            y = self.y(j)
            prev = -math.inf
            for lo, hi in _poly_row(poly, y):
                self.fill(buf, j, prev, lo, value)
                prev = hi
            self.fill(buf, j, prev, math.inf, value)
        for (ax, ay), (bx, by) in _edges(poly):
            for j in self.rows(min(ay, by) - t, max(ay, by) + t):
                iv = _capsule_row(ax, ay, bx, by, t, self.y(j))
                if iv is not None:
                    self.fill(buf, j, iv[0], iv[1], value)

    def frame(self, buf: bytearray, value: int) -> None:
        """The outermost ring of nodes (the search never steps off the window)."""
        w, h = self.w, self.h
        buf[0:w] = bytes([value]) * w
        buf[(h - 1) * w : h * w] = bytes([value]) * w
        for j in range(h):
            buf[j * w] = value
            buf[j * w + w - 1] = value


def _lattice(lo: float, hi: float, anchor: float, pitch: float) -> tuple[float, int]:
    """Origin (aligned so `anchor` is a node) and node count covering [lo, hi]."""
    origin = anchor - math.floor((anchor - lo) / pitch + 1e-6) * pitch
    n = math.floor((hi - origin) / pitch + 1e-6) + 1
    return origin, max(n, 1)


def build_grid(
    model: BoardModel,
    req: RouteRequest,
    *,
    pitch: float = 0.05,
    margin: float = 0.0,
    window_mm: float | None = None,
    extra: Sequence[tuple[str, Shape]] = (),
    extra_vias: Sequence[Shape] = (),
    deadline: float | None = None,
) -> ObstacleGrid:
    """The obstacle grid for `req` (B-C12). `extra` = additional (layer, shape) track
    obstacles and `extra_vias` additional via obstacles (need = via-centre distance) — the
    pair fallback's partner. Raises `RouteTooLarge` before allocating when the window
    exceeds `MAX_CELLS_PER_LAYER`; `ValueError` for an empty, duplicate or unknown layer."""
    if not (pitch > 0 and math.isfinite(pitch)):
        raise ValueError("pitch must be positive")
    nums = (
        req.width,
        req.clearance,
        margin,
        req.via[0],
        req.via[1],
        req.a.x,
        req.a.y,
        req.b.x,
        req.b.y,
    )
    if not all(math.isfinite(v) for v in nums):
        raise ValueError("request numbers must be finite")
    if req.width <= 0 or req.clearance < 0 or margin < 0 or not req.via[0] > req.via[1] > 0:
        raise ValueError("width > 0, clearance >= 0, margin >= 0, via diameter > drill > 0")
    if max(req.width, req.clearance, margin, req.via[0]) > MAX_SIZE_MM:
        raise ValueError(f"width, clearance, margin and via diameter must be <= {MAX_SIZE_MM} mm")
    if not req.layers or len(set(req.layers)) != len(req.layers):
        raise ValueError("request layers must be non-empty and unique")
    if model.copper_layers and not set(req.layers) <= set(model.copper_layers):
        raise ValueError(f"unknown copper layer in {req.layers}")
    win = WINDOW_MM if window_mm is None else window_mm
    wx0, wy0, wx1, wy1 = window_bounds(model, [req], win)
    # a pad outside the outline bbox (edge overhang) stays two nodes inside the window
    pad_x = (req.a.x, req.b.x)
    pad_y = (req.a.y, req.b.y)
    wx0, wy0 = min(wx0, min(pad_x) - 2 * pitch), min(wy0, min(pad_y) - 2 * pitch)
    wx1, wy1 = max(wx1, max(pad_x) + 2 * pitch), max(wy1, max(pad_y) + 2 * pitch)
    x0, nx = _lattice(wx0, wx1, req.a.x, pitch)
    y0, ny = _lattice(wy0, wy1, req.a.y, pitch)
    cells_n = nx * ny
    if cells_n > MAX_CELLS_PER_LAYER:
        raise RouteTooLarge(cells_n)

    layers = tuple(req.layers)
    mk = _Marker(x0, y0, nx, ny, pitch)
    r = req.clearance + margin + req.width / 2
    via_r = req.via[0] / 2
    via_need = via_r + req.clearance + margin
    bx0, by0 = x0 - 5.0, y0 - 5.0  # generous bbox filter for items
    bx1, by1 = x0 + nx * pitch + 5.0, y0 + ny * pitch + 5.0

    def near(box: tuple[float, float, float, float], grow: float) -> bool:
        return not (
            box[2] + grow < bx0 or box[0] - grow > bx1 or box[3] + grow < by0 or box[1] - grow > by1
        )

    copper = list(model.copper_layers)
    pads = [pad for part in model.parts for pad in part.pads]
    shapes: list[list[Shape]] = [[] for _ in layers]
    own: list[list[Shape]] = [[] for _ in layers]
    via_shapes: list[Shape] = []  # cores a via must keep `via_need` from (any layer)
    via_own: list[Shape] = []  # own SMD pads: no via in a pad (reflow), keep via_r
    via_keepouts: list[Shape] = []

    for pad in pads:
        on = [k for k, name in enumerate(layers) if _pad_on_layer(pad, name)]
        anywhere = any(_pad_on_layer(pad, name) for name in copper) or bool(on)
        mine = pad.net == req.net and pad.net != ""
        if mine:
            s = _pad_shape(pad, 0.0)
            if on and near(_shape_bbox(s), 0.0):
                for k in on:
                    own[k].append(s)
            if pad.smd and anywhere:
                via_own.append(_pad_shape(pad, via_r))
            continue
        s = _pad_shape(pad, r)
        if not near(_shape_bbox(s), r + via_need):
            continue
        for k in on:
            shapes[k].append(s)
        if anywhere:  # a through via touches every copper layer
            via_shapes.append(_pad_shape(pad, via_need))
    for t in model.tracks:
        box = (
            min(t.start[0], t.end[0]),
            min(t.start[1], t.end[1]),
            max(t.start[0], t.end[0]),
            max(t.start[1], t.end[1]),
        )
        if not near(box, r + via_need + t.width):
            continue
        if t.net == req.net and t.net != "":
            if t.layer in layers:
                own[layers.index(t.layer)].append(("s", *t.start, *t.end, t.width / 2))
            continue
        if t.layer in layers:
            shapes[layers.index(t.layer)].append(("s", *t.start, *t.end, r + t.width / 2))
        via_shapes.append(("s", *t.start, *t.end, via_need + t.width / 2))
    for a in model.arcs:
        if a.net == req.net and a.net != "":
            continue
        chords, sag = _arc_points(a)
        k = layers.index(a.layer) if a.layer in layers else -1
        for p, q in itertools.pairwise(chords):
            if k >= 0:
                shapes[k].append(("s", *p, *q, r + a.width / 2 + sag))
            via_shapes.append(("s", *p, *q, via_need + a.width / 2 + sag))
    for v in model.vias:
        if not near((v.at[0], v.at[1], v.at[0], v.at[1]), r + via_need + v.diameter):
            continue
        if v.net == req.net and v.net != "":
            for k in range(len(layers)):
                own[k].append(("d", *v.at, v.diameter / 2, 0.0))
            continue
        for k in range(len(layers)):
            shapes[k].append(("d", *v.at, v.diameter / 2, r))
        via_shapes.append(("d", *v.at, v.diameter / 2, via_need))
    for g in model.graphics:
        if g.layer not in copper and g.layer not in layers:
            continue
        for s in _graphic_shapes(g.kind, g.points, r + g.width / 2):
            if g.layer in layers:
                shapes[layers.index(g.layer)].append(s)
            via_shapes.append((*s[:-1], _shape_need(s) - r + via_need))
    for z in model.zones:
        if not z.rule_area or len(z.outline) < 3:
            continue
        zl = _layers_of(z.layers, copper)
        if "tracks" in z.keepout:
            for k, name in enumerate(layers):
                if name in zl:
                    shapes[k].append(("p", tuple(z.outline), req.width / 2 + margin))
        if "vias" in z.keepout and zl & (set(copper) | set(layers)):  # through vias
            via_keepouts.append(("p", tuple(z.outline), via_r + margin))

    for s in extra_shapes(extra, layers):
        shapes[s[0]].append(s[1])

    check_deadline(deadline)
    cells = tuple(bytearray(cells_n) for _ in layers)
    for k in range(len(layers)):
        for s in own[k]:
            mk.shape(cells[k], s, _shape_need(s), OWN)
        for count, s in enumerate(shapes[k]):
            if not count % 256:
                check_deadline(deadline)
            need = _shape_need(s)
            mk.shape(cells[k], s, need + chord_slack(need, pitch), BLOCKED)
    poly = tuple(model.outline.polygon) if model.outline.kind != "none" else ()
    inset = edge_clearance(model) + req.width / 2 + margin
    if len(poly) >= 3:  # rasterised once, copied into every layer
        outside = bytearray(cells_n)
        mk.outside(outside, poly, inset + chord_slack(inset, pitch), BLOCKED)
        runs = [(m.start(), m.end()) for m in re.finditer(rb"\x02+", outside)]
        for k in range(len(layers)):
            for lo, hi in runs:
                cells[k][lo:hi] = outside[lo:hi]
    for k in range(len(layers)):
        mk.frame(cells[k], BLOCKED)

    check_deadline(deadline)
    via_blocked = bytearray(cells_n)
    via_obstacles = tuple(via_shapes + via_own + via_keepouts + list(extra_vias))
    if req.allow_vias:
        for s in via_obstacles:
            mk.shape(via_blocked, s, _shape_need(s), 1)
        if len(poly) >= 3:
            mk.outside(via_blocked, poly, edge_clearance(model) + via_r + margin, 1)
        mk.frame(via_blocked, 1)
    else:
        via_blocked[:] = b"\x01" * cells_n

    return ObstacleGrid(
        pitch=pitch,
        x0=x0,
        y0=y0,
        w=nx,
        h=ny,
        layers=layers,
        cells=cells,
        via_blocked=via_blocked,
        shapes=tuple(tuple(s) for s in shapes),
        outline=poly if len(poly) >= 3 else (),
        inset=inset,
        via_shapes=via_obstacles if req.allow_vias else (),
        via_inset=edge_clearance(model) + via_r + margin,
    )


def extra_shapes(
    extra: Sequence[tuple[str, Shape]], layers: Sequence[str]
) -> list[tuple[int, Shape]]:
    return [(list(layers).index(name), s) for name, s in extra if name in layers]


def band_cells(
    grid: ObstacleGrid, segments: Sequence[tuple[str, Point, Point]], lo: float, hi: float
) -> tuple[bytearray, ...]:
    """Per layer of `grid`: 1 at the nodes whose distance to one of `segments` on that layer
    lies in [lo, hi] (the pair fallback's preferred band), else 0."""
    mk = _Marker(grid.x0, grid.y0, grid.w, grid.h, grid.pitch)
    out = []
    for name in grid.layers:
        buf = bytearray(grid.w * grid.h)
        mine = [(p, q) for layer, p, q in segments if layer == name]
        for p, q in mine:
            mk.shape(buf, ("s", *p, *q, 0.0), hi, 1)
        for p, q in mine:
            if lo > 0:
                mk.shape(buf, ("s", *p, *q, 0.0), lo - 1e-9, 0)
        out.append(buf)
    return tuple(out)


PRUNE_HALO_MM = 3.0  # beyond any obstacle's reach (clearance + widths + via radius)


def _bbox_hits(
    box: tuple[float, float, float, float], win: tuple[float, float, float, float]
) -> bool:
    return not (box[2] < win[0] or box[0] > win[2] or box[3] < win[1] or box[1] > win[3])


def _pts_box(pts: Iterable[Point]) -> tuple[float, float, float, float]:
    pts = list(pts)
    if not pts:
        return (math.inf, math.inf, -math.inf, -math.inf)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def prune_model(
    model: BoardModel, req: RouteRequest | tuple[RouteRequest, RouteRequest]
) -> BoardModel:
    """`model` reduced to what can matter for `req` (or a pair): outline, rules, net classes
    and layers stay; pads (parts with at least one), tracks, arcs, vias, zones (outline only,
    no fills) and graphics only when they intersect the retry window (2 x WINDOW_MM around
    the requests' pads) grown by PRUNE_HALO_MM. Pure; the result routes exactly like the
    full model."""
    reqs = list(req) if isinstance(req, tuple) else [req]
    x0, y0, x1, y1 = window_bounds(model, reqs, 2 * WINDOW_MM)
    for r in reqs:  # build_grid keeps off-outline pads inside the window
        for p in (r.a, r.b):
            x0, y0, x1, y1 = min(x0, p.x), min(y0, p.y), max(x1, p.x), max(y1, p.y)
    win = (x0 - PRUNE_HALO_MM, y0 - PRUNE_HALO_MM, x1 + PRUNE_HALO_MM, y1 + PRUNE_HALO_MM)

    def pad_hits(p: Pad) -> bool:
        r = math.hypot(p.w, p.h) / 2
        return _bbox_hits((p.x - r, p.y - r, p.x + r, p.y + r), win)

    parts = []
    for part in model.parts:
        pads = tuple(p for p in part.pads if pad_hits(p))
        if pads:
            parts.append(replace(part, pads=pads))
    tracks = tuple(
        t for t in model.tracks if _bbox_hits(_grow(_pts_box((t.start, t.end)), t.width / 2), win)
    )
    arcs = tuple(
        a
        for a in model.arcs
        if _bbox_hits(
            _grow(_pts_box((a.start, a.mid, a.end)), a.width / 2 + math.dist(a.start, a.end)), win
        )
    )
    vias = tuple(v for v in model.vias if _bbox_hits(_grow(_pts_box((v.at,)), v.diameter / 2), win))
    zones = tuple(
        replace(z, filled=(), filled_holes=(), filled_layers=())
        for z in model.zones
        if _bbox_hits(_pts_box(z.outline), win)
    )
    graphics = tuple(
        g
        for g in model.graphics
        if _bbox_hits(_grow(_pts_box(g.points), g.width / 2 + _circle_reach(g)), win)
    )
    return replace(
        model,
        parts=tuple(parts),
        tracks=tracks,
        arcs=arcs,
        vias=vias,
        zones=zones,
        graphics=graphics,
    )


def _grow(b: tuple[float, float, float, float], d: float) -> tuple[float, float, float, float]:
    return (b[0] - d, b[1] - d, b[2] + d, b[3] + d)


def _circle_reach(g: GraphicItem) -> float:
    if g.kind == "circle" and len(g.points) == 2:
        return math.dist(g.points[0], g.points[1])
    return 0.0
