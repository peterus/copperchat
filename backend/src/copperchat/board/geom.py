"""Pure 2-D geometry for the board layer (stdlib only). Units mm, board coordinates."""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence

Point = tuple[float, float]
Poly = Sequence[Point]

_EPS = 1e-9


def octi_dist(a: Point, b: Point) -> float:
    """Length of the shortest 0/45/90-degree path between `a` and `b`."""
    dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
    return max(dx, dy) + (math.sqrt(2) - 1) * min(dx, dy)


def polygon_area(pts: Poly) -> float:
    """Absolute area (shoelace)."""
    n = len(pts)
    s = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _cross(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _on_segment(p: Point, a: Point, b: Point) -> bool:
    if abs(_cross(a, b, p)) > _EPS * max(1.0, math.dist(a, b)):
        return False
    return (
        min(a[0], b[0]) - _EPS <= p[0] <= max(a[0], b[0]) + _EPS
        and min(a[1], b[1]) - _EPS <= p[1] <= max(a[1], b[1]) + _EPS
    )


def _on_boundary(p: Point, pts: Poly) -> bool:
    n = len(pts)
    return any(_on_segment(p, pts[i], pts[(i + 1) % n]) for i in range(n))


def point_in_polygon(p: Point, pts: Poly) -> bool:
    """True inside or on the boundary (ray casting)."""
    if _on_boundary(p, pts):
        return True
    x, y = p
    inside = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _strictly_inside(p: Point, pts: Poly) -> bool:
    return point_in_polygon(p, pts) and not _on_boundary(p, pts)


def _proper_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Segments ab and cd cross at a single interior point of both."""
    d1, d2 = _cross(a, b, c), _cross(a, b, d)
    d3, d4 = _cross(c, d, a), _cross(c, d, b)
    return ((d1 > _EPS and d2 < -_EPS) or (d1 < -_EPS and d2 > _EPS)) and (
        (d3 > _EPS and d4 < -_EPS) or (d3 < -_EPS and d4 > _EPS)
    )


def _edges(pts: Poly) -> list[tuple[Point, Point]]:
    return [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]


def _any_proper_cross(a: Poly, b: Poly) -> bool:
    eb = _edges(b)
    return any(_proper_cross(p, q, r, s) for p, q in _edges(a) for r, s in eb)


def _midpoints(pts: Poly) -> list[Point]:
    return [((p[0] + q[0]) / 2, (p[1] + q[1]) / 2) for p, q in _edges(pts)]


def polygon_inside(inner: Poly, outer: Poly) -> bool:
    """`inner` lies fully inside `outer` (boundary contact allowed); works for concave."""
    if len(inner) < 3 or len(outer) < 3:
        return False
    if not all(point_in_polygon(p, outer) for p in inner):
        return False
    if _any_proper_cross(inner, outer):
        return False
    return all(point_in_polygon(m, outer) for m in _midpoints(inner))


def _is_convex(pts: Poly) -> bool:
    n = len(pts)
    sign = 0
    for i in range(n):
        c = _cross(pts[i], pts[(i + 1) % n], pts[(i + 2) % n])
        if abs(c) <= _EPS:
            continue
        s = 1 if c > 0 else -1
        if sign and s != sign:
            return False
        sign = s
    return True


def _sat_overlap(a: Poly, b: Poly) -> bool:
    """Separating-axis test for two convex polygons; touching is not overlap."""
    for poly in (a, b):
        for p, q in _edges(poly):
            nx, ny = q[1] - p[1], p[0] - q[0]
            pa = [nx * v[0] + ny * v[1] for v in a]
            pb = [nx * v[0] + ny * v[1] for v in b]
            tol = _EPS * max(1.0, math.hypot(nx, ny))
            if max(pa) <= min(pb) + tol or max(pb) <= min(pa) + tol:
                return False
    return True


def polygons_overlap(a: Poly, b: Poly) -> bool:
    """Interiors intersect (touching edges do not count). SAT for convex pairs, edge
    crossing and containment tests otherwise."""
    if len(a) < 3 or len(b) < 3:
        return False
    if _is_convex(a) and _is_convex(b):
        return _sat_overlap(a, b)
    if _any_proper_cross(a, b):
        return True
    # No proper crossings: the interiors intersect iff some boundary piece of one polygon,
    # probed a hair to its own interior side, lies strictly inside the other.
    return _boundary_enters(a, b) or _boundary_enters(b, a)


def _signed_area(pts: Poly) -> float:
    return sum(
        pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
        for i in range(len(pts))
    )


def _boundary_enters(a: Poly, b: Poly) -> bool:
    sign = 1.0 if _signed_area(a) > 0 else -1.0
    for p, q in _edges(a):
        length = math.dist(p, q)
        if length < _EPS:
            continue
        # split the edge at b's vertices lying on it
        ts = {0.0, 1.0}
        for v in b:
            if _on_segment(v, p, q):
                ts.add(math.dist(p, v) / length)
        cuts = sorted(ts)
        nx, ny = -(q[1] - p[1]) / length * sign, (q[0] - p[0]) / length * sign
        eps = 1e-6
        for t0, t1 in itertools.pairwise(cuts):
            if (t1 - t0) * length < 1e-7:
                continue
            tm = (t0 + t1) / 2
            m = (p[0] + (q[0] - p[0]) * tm, p[1] + (q[1] - p[1]) * tm)
            probe = (m[0] + nx * eps, m[1] + ny * eps)
            if _strictly_inside(probe, a) and _strictly_inside(probe, b):
                return True
    return False


def convex_hull(pts: Poly) -> list[Point]:
    """Counter-clockwise convex hull (monotone chain), collinear points dropped."""
    uniq = sorted(set(pts))
    if len(uniq) <= 2:
        return list(uniq)
    lower: list[Point] = []
    for p in uniq:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[Point] = []
    for p in reversed(uniq):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def rect_poly(x: float, y: float, w: float, h: float, r: float = 0.0) -> list[Point]:
    """Rectangle with top-left (x, y); `r` > 0 rounds the corners with 8 segments per quarter."""
    if r <= 0:
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    r = min(r, w / 2, h / 2)
    out: list[Point] = []
    # corner centre, start angle (degrees); y points down, walk clockwise on screen
    corners = (
        (x + w - r, y + r, -90.0),
        (x + w - r, y + h - r, 0.0),
        (x + r, y + h - r, 90.0),
        (x + r, y + r, 180.0),
    )
    for cx, cy, a0 in corners:
        for i in range(9):
            a = math.radians(a0 + 90.0 * i / 8)
            out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return out
