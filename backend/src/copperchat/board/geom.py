"""Pure 2-D geometry for the board layer (stdlib only). Units mm, board coordinates."""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from copperchat.board.model import BoardModel, Pad, Track, Via

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


# --- B-C5: the one bend definition (M4/M7) ------------------------------------------------

ANGLE_TOL_DEG = 0.5
_JOIN_TOL = 0.002  # mm: endpoints closer than this are one node
_ZERO_LEN = 1e-6  # mm: shorter segments are dropped


def CHAMFER_MIN(w: float) -> float:
    """Shortest straight piece between two same-sense 45-degree bends."""
    return max(1.0 * w, 0.25)


def JOG_MIN(w: float) -> float:
    """Shortest straight piece between two opposite-sense 45-degree bends."""
    return 2.0 * w


@dataclass(frozen=True)
class BendReport:
    non_octilinear: tuple[Point, ...]  # segment midpoints off 0/45/90/... by > 0.5 deg
    ninety: tuple[Point, ...]  # vertices with a 90 (+- 0.5) deg direction change
    acute: tuple[Point, ...]  # vertices with a change >= 134.5 deg
    off_angle: tuple[Point, ...]  # every other failing vertex (e.g. 46, 60, 90.8 deg)
    t_junctions: tuple[Point, ...]  # off-pad branch meeting at 90 deg
    micro_jogs: tuple[Point, ...]
    short_chamfers: tuple[Point, ...]
    bends: int  # 45-degree bends (pad entries excluded)


def _heading(a: Point, b: Point) -> float:
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))


def _turn(a: Point, b: Point, c: Point) -> tuple[float, float]:
    """Direction change at `b` in [0, 180] degrees and the cross product's sign."""
    ux, uy = b[0] - a[0], b[1] - a[1]
    vx, vy = c[0] - b[0], c[1] - b[1]
    cross = ux * vy - uy * vx
    dot = ux * vx + uy * vy
    return math.degrees(math.atan2(abs(cross), dot)), (1.0 if cross > 0 else -1.0)


def _mid(a: Point, b: Point) -> Point:
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)


def _off_octilinear(a: Point, b: Point) -> bool:
    h = _heading(a, b) % 45.0
    return min(h, 45.0 - h) > ANGLE_TOL_DEG


def _vertex_kind(delta: float) -> str | None:
    """None (collinear), "bend" (45 deg) or the failure field of a direction change."""
    if delta <= ANGLE_TOL_DEG:
        return None
    if abs(delta - 45.0) <= ANGLE_TOL_DEG:
        return "bend"
    if abs(delta - 90.0) <= ANGLE_TOL_DEG:
        return "ninety"
    if delta >= 135.0 - ANGLE_TOL_DEG:
        return "acute"
    return "off_angle"


def analyse_bends(
    points: Sequence[Point], width: float, *, pad_ends: tuple[bool, bool]
) -> BendReport:
    """Classify every segment and vertex of one polyline (B-C5; no gaps)."""
    pts: list[Point] = []
    for p in points:
        if not pts or math.dist(pts[-1], p) >= _ZERO_LEN:
            pts.append(p)
    non_octi = [_mid(a, b) for a, b in itertools.pairwise(pts) if _off_octilinear(a, b)]
    ninety: list[Point] = []
    acute: list[Point] = []
    off: list[Point] = []
    # non-collinear vertices in path order: (index, kind, sense); kind "bend" or "fail"
    verts: list[tuple[int, str, float]] = []
    fails = {"ninety": ninety, "acute": acute, "off_angle": off}
    for i in range(1, len(pts) - 1):
        delta, sense = _turn(pts[i - 1], pts[i], pts[i + 1])
        kind = _vertex_kind(delta)
        if kind is None:
            continue
        if kind == "bend":
            verts.append((i, "bend", sense))
            continue
        fails[kind].append(pts[i])
        verts.append((i, "fail", sense))
    exempt: set[int] = set()
    if verts and pad_ends[0]:
        exempt.add(verts[0][0])
    if verts and pad_ends[1]:
        exempt.add(verts[-1][0])
    bends = sum(1 for i, kind, _ in verts if kind == "bend" and i not in exempt)
    jogs: list[Point] = []
    chamfers: list[Point] = []
    cum = [0.0]
    for a, b in itertools.pairwise(pts):
        cum.append(cum[-1] + math.dist(a, b))
    for (i, ki, si), (j, kj, sj) in itertools.pairwise(verts):
        if ki != "bend" or kj != "bend" or i in exempt or j in exempt:
            continue
        piece = cum[j] - cum[i] + _EPS  # float noise only: exactly the minimum passes
        if si == sj and piece < CHAMFER_MIN(width):
            chamfers.append(_mid(pts[i], pts[j]))
        elif si != sj and piece < JOG_MIN(width):
            jogs.append(_mid(pts[i], pts[j]))
    return BendReport(
        non_octilinear=tuple(non_octi),
        ninety=tuple(ninety),
        acute=tuple(acute),
        off_angle=tuple(off),
        t_junctions=(),
        micro_jogs=tuple(jogs),
        short_chamfers=tuple(chamfers),
        bends=bends,
    )


def _pad_on_layer(pad: Pad, layer: str) -> bool:
    if layer in pad.layers or "*.Cu" in pad.layers:
        return True
    # a plated through-hole spans every copper layer even if only the outer ones are listed
    return pad.drill is not None and not pad.smd and any(n.endswith(".Cu") for n in pad.layers)


def _in_pad(p: Point, pad: Pad) -> bool:
    """`p` inside the pad rectangle (w x h about its centre, rotated by `rot` degrees,
    counter-clockwise on screen with y down); boundary inclusive within the join tolerance."""
    dx, dy = p[0] - pad.x, p[1] - pad.y
    t = math.radians(pad.rot)
    c, s = math.cos(t), math.sin(t)
    lx, ly = dx * c - dy * s, dx * s + dy * c
    return abs(lx) <= pad.w / 2 + _JOIN_TOL and abs(ly) <= pad.h / 2 + _JOIN_TOL


def _layer_pads(model: BoardModel, layer: str) -> list[Pad]:
    return [pad for part in model.parts for pad in part.pads if _pad_on_layer(pad, layer)]


BBox = tuple[float, float, float, float]  # x0, y0, x1, y1


class _Grid:
    """Uniform bucket grid (cell `cell` mm) of item ids keyed by their bounding boxes."""

    def __init__(self, cell: float = 2.0) -> None:
        self.cell = cell
        self.cells: dict[tuple[int, int], list[int]] = {}

    def _keys(self, b: BBox) -> Iterator[tuple[int, int]]:
        c = self.cell
        for gx in range(math.floor(b[0] / c), math.floor(b[2] / c) + 1):
            for gy in range(math.floor(b[1] / c), math.floor(b[3] / c) + 1):
                yield gx, gy

    def add(self, item: int, b: BBox) -> None:
        for k in self._keys(b):
            self.cells.setdefault(k, []).append(item)

    def query(self, b: BBox) -> list[int]:
        seen: set[int] = set()
        for k in self._keys(b):
            seen.update(self.cells.get(k, ()))
        return sorted(seen)


class _Near:
    """Pads and vias bucketed by their reach: which pads contain a point (`_in_pad`), and
    whether a point lies on a via (within its radius, at least the join tolerance)."""

    def __init__(self, pads: Sequence[Pad], vias: Sequence[Via]) -> None:
        self.pads, self.vias = list(pads), list(vias)
        self.pad_grid, self.via_grid = _Grid(), _Grid()
        for i, q in enumerate(self.pads):
            r = math.hypot(q.w, q.h) / 2 + _JOIN_TOL
            self.pad_grid.add(i, (q.x - r, q.y - r, q.x + r, q.y + r))
        for i, v in enumerate(self.vias):
            r = max(v.diameter / 2, _JOIN_TOL)
            self.via_grid.add(i, (v.at[0] - r, v.at[1] - r, v.at[0] + r, v.at[1] + r))

    def in_pad(self, p: Point) -> bool:
        box = (p[0], p[1], p[0], p[1])
        return any(_in_pad(p, self.pads[i]) for i in self.pad_grid.query(box))

    def at_via(self, p: Point) -> bool:
        box = (p[0], p[1], p[0], p[1])
        return any(
            math.dist(p, (v := self.vias[i]).at) <= max(v.diameter / 2, _JOIN_TOL)
            for i in self.via_grid.query(box)
        )


class _Nodes:
    """Snaps points to canonical nodes within the join tolerance (grid hashed)."""

    def __init__(self) -> None:
        self._grid: dict[tuple[int, int], list[Point]] = {}

    def _key(self, p: Point) -> tuple[int, int]:
        return (math.floor(p[0] / _JOIN_TOL), math.floor(p[1] / _JOIN_TOL))

    def find(self, p: Point) -> Point | None:
        kx, ky = self._key(p)
        best: Point | None = None
        for gx in (kx - 1, kx, kx + 1):
            for gy in (ky - 1, ky, ky + 1):
                for q in self._grid.get((gx, gy), ()):
                    if math.dist(p, q) <= _JOIN_TOL and (best is None or q < best):
                        best = q
        return best

    def snap(self, p: Point) -> Point:
        q = self.find(p)
        if q is not None:
            return q
        self._grid.setdefault(self._key(p), []).append(p)
        return p


def _interior_t(p: Point, a: Point, b: Point) -> float | None:
    """Parameter of `p` on the interior of segment ab (within the join tolerance)."""
    vx, vy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(vx, vy)
    t = ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / (length * length)
    if t * length <= _JOIN_TOL or (1.0 - t) * length <= _JOIN_TOL:
        return None
    proj = (a[0] + t * vx, a[1] + t * vy)
    return t if math.dist(proj, p) <= _JOIN_TOL else None


_Seg = tuple[Point, Point, float]
_Graph = tuple[list[_Seg], dict[Point, list[int]]]


def _graph(tracks: Iterable[Track], centres: Iterable[Point]) -> _Graph:
    """Snapped segments of one net/layer (`tracks` are that net/layer's), split where an
    endpoint or one of `centres` (the net's via centres and pad centres on the layer) lies on
    a segment's interior (a T), and node -> incident segment ids. Splitters are bucketed:
    each segment only tests the points near it."""
    nodes = _Nodes()
    raw: list[_Seg] = []
    for t in tracks:
        if math.dist(t.start, t.end) < _ZERO_LEN:
            continue
        a, b = nodes.snap(t.start), nodes.snap(t.end)
        if a != b:
            raw.append((a, b, t.width))
    splitters = sorted({nodes.snap(c) for c in centres} | {p for a, b, _ in raw for p in (a, b)})
    grid = _Grid()
    for i, p in enumerate(splitters):
        grid.add(i, (p[0], p[1], p[0], p[1]))
    segs: list[_Seg] = []
    for a, b, w in raw:
        box = (
            min(a[0], b[0]) - _JOIN_TOL,
            min(a[1], b[1]) - _JOIN_TOL,
            max(a[0], b[0]) + _JOIN_TOL,
            max(a[1], b[1]) + _JOIN_TOL,
        )
        cuts = sorted(
            (tp, splitters[i])
            for i in grid.query(box)
            if (tp := _interior_t(splitters[i], a, b)) is not None
        )
        chain = [a, *(p for _, p in cuts), b]
        segs.extend((p, q, w) for p, q in itertools.pairwise(chain) if p != q)
    segs = sorted({(min(a, b), max(a, b), w) for a, b, w in segs})
    adj: dict[Point, list[int]] = {}
    for i, (a, b, _) in enumerate(segs):
        adj.setdefault(a, []).append(i)
        adj.setdefault(b, []).append(i)
    return segs, adj


def _drop_collinear(pts: list[Point]) -> list[Point]:
    out: list[Point] = []
    for p in pts:
        if out and math.dist(out[-1], p) < _ZERO_LEN:
            continue
        while len(out) >= 2 and _turn(out[-2], out[-1], p)[0] <= ANGLE_TOL_DEG:
            out.pop()
        out.append(p)
    return out


def _walk_polylines(g: _Graph, near: _Near) -> list[tuple[tuple[Point, ...], float]]:
    """The polylines of one net/layer graph; `near` holds the net's vias and its pads on
    the layer (polyline ends)."""
    segs, adj = g

    def terminal(n: Point) -> bool:
        inc = adj[n]
        if len(inc) != 2 or segs[inc[0]][2] != segs[inc[1]][2]:
            return True
        return near.at_via(n) or near.in_pad(n)

    used: set[int] = set()
    out: list[tuple[tuple[Point, ...], float]] = []

    def walk(start: Point, first: int) -> None:
        chain = [start]
        node, seg = start, first
        while True:
            used.add(seg)
            a, b, _ = segs[seg]
            node = b if node == a else a
            chain.append(node)
            if node == start or terminal(node):
                break
            nxt = [i for i in adj[node] if i not in used]
            if not nxt:
                break
            seg = nxt[0]
        pts = _drop_collinear(chain)
        if pts[-1] < pts[0]:
            pts.reverse()
        out.append((tuple(pts), segs[first][2]))

    for n in sorted(adj):
        if terminal(n):
            for i in adj[n]:
                if i not in used:
                    walk(n, i)
    for n in sorted(adj):  # closed loops without a terminal node
        for i in adj[n]:
            if i not in used:
                walk(n, i)
    return sorted(out)


def _centres(vias: Iterable[Via], pads: Iterable[Pad]) -> list[Point]:
    return [v.at for v in vias] + [(p.x, p.y) for p in pads]


def polylines(model: BoardModel, net: str, layer: str) -> list[tuple[tuple[Point, ...], float]]:
    """(points, width) of the straight tracks of `net` on `layer`: segments joined at
    coincident endpoints (0.002 mm), T-split, split at nodes of degree != 2, at vias, inside
    same-net pads and where the width changes; collinear interior vertices dropped."""
    vias = [v for v in model.vias if v.net == net]
    pads = [p for p in _layer_pads(model, layer) if p.net == net]
    tracks = [t for t in model.tracks if t.net == net and t.layer == layer]
    return _walk_polylines(_graph(tracks, _centres(vias, pads)), _Near(pads, vias))


def _sorted(points: Iterable[Point]) -> tuple[Point, ...]:
    return tuple(sorted(points))


def analyse_tracks(model: BoardModel) -> BendReport:
    """`analyse_bends` over every polyline of every net/layer (pad entries where a polyline
    end lies in a same-net pad on that layer), plus T-junctions and arcs (non-octilinear).
    Non-octilinear is tested per raw track (before collinear merging); the direction change
    at a degree-2 node where polylines end (width change, same-layer via, closed-loop start)
    is classified too unless the node lies in a same-net pad."""
    acc: dict[str, list[Point]] = {
        k: []
        for k in (
            "non_octilinear",
            "ninety",
            "acute",
            "off_angle",
            "t_junctions",
            "micro_jogs",
            "short_chamfers",
        )
    }
    bends = 0
    by_net_layer: dict[tuple[str, str], list[Track]] = {}
    for t in model.tracks:
        by_net_layer.setdefault((t.net, t.layer), []).append(t)
    vias_of: dict[str, list[Via]] = {}
    for v in model.vias:
        vias_of.setdefault(v.net, []).append(v)
    layer_near: dict[str, tuple[_Near, dict[str, list[Pad]]]] = {}
    for net, layer in sorted(by_net_layer):
        if layer not in layer_near:
            layer_pads = _layer_pads(model, layer)
            pads_of: dict[str, list[Pad]] = {}
            for q in layer_pads:
                pads_of.setdefault(q.net, []).append(q)
            layer_near[layer] = (_Near(layer_pads, model.vias), pads_of)
        every, pads_of = layer_near[layer]  # every pad on the layer, every via
        pads, vias = pads_of.get(net, []), vias_of.get(net, [])
        near = _Near(pads, vias)  # the net's own
        g = _graph(by_net_layer[(net, layer)], _centres(vias, pads))
        segs, adj = g
        line_ends: set[Point] = set()
        for pts, width in _walk_polylines(g, near):
            ends = (near.in_pad(pts[0]), near.in_pad(pts[-1]))
            r = analyse_bends(pts, width, pad_ends=ends)
            for k in acc:
                if k != "non_octilinear":  # tested per raw track below
                    acc[k].extend(getattr(r, k))
            bends += r.bends
            line_ends.update((pts[0], pts[-1]))
        for node in sorted(line_ends):
            inc = adj.get(node, [])
            if len(inc) != 2 or near.in_pad(node):
                continue
            far = [b if node == a else a for a, b, _ in (segs[i] for i in inc)]
            kind = _vertex_kind(_turn(far[0], node, far[1])[0])
            if kind == "bend":
                bends += 1
            elif kind is not None:
                acc[kind].append(node)
        for node, inc in adj.items():
            if len(inc) < 3:
                continue
            if every.in_pad(node) or every.at_via(node):
                continue
            heads = []
            for i in inc:
                a, b, _ = segs[i]
                heads.append(_heading(node, b if node == a else a))
            for h1, h2 in itertools.combinations(heads, 2):
                d = abs(h1 - h2) % 360.0
                if abs(min(d, 360.0 - d) - 90.0) <= ANGLE_TOL_DEG:
                    acc["t_junctions"].append(node)
                    break
    acc["non_octilinear"].extend(
        _mid(t.start, t.end)
        for t in model.tracks
        if math.dist(t.start, t.end) >= _ZERO_LEN and _off_octilinear(t.start, t.end)
    )
    acc["non_octilinear"].extend(a.mid for a in model.arcs)
    return BendReport(
        non_octilinear=_sorted(acc["non_octilinear"]),
        ninety=_sorted(acc["ninety"]),
        acute=_sorted(acc["acute"]),
        off_angle=_sorted(acc["off_angle"]),
        t_junctions=_sorted(acc["t_junctions"]),
        micro_jogs=_sorted(acc["micro_jogs"]),
        short_chamfers=_sorted(acc["short_chamfers"]),
        bends=bends,
    )
