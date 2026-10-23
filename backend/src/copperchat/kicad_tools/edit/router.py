"""Orthogonal wire router with T-joins (E5, spec §5.2).

Port of spike 2D `geo.route` / `path_problems`, extended after 2D failure mode 4: a
route may end on a wire of the target net (T-join, junction), and items of the nets
being joined are contacts, not obstacles. A route never touches a connection point of
another net (pin end, wire end, label, junction), never runs along or turns on a wire
of another net (all of these would connect it, spike 2A Q3), never enters a symbol body,
keeps the sheet-edge clearance (`sheet_edges`, #9), and has at most `max_segments`
segments. Every 1-3 segment orthogonal path is either straight, an L or a Z (H-V-H /
V-H-V), so the candidates below enumerate all of them within a window around both ends.
"""

from __future__ import annotations

import bisect
import itertools
import math
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from copperchat.kicad_tools.access import Netlist
from copperchat.kicad_tools.edit.geometry import (
    EPS_MM,
    GRID_MM,
    Box,
    PinEnd,
    Point,
    in_segment_interior,
    on_grid,
    on_segment,
    same_point,
    snap,
)
from copperchat.kicad_tools.edit.scene import Scene, WireInfo
from copperchat.kicad_tools.edit.sheet_edges import (
    SHEET_EDGE_CLEARANCE_MM,
    SheetBox,
    sheet_boxes,
    sheet_edge_problems,
    sheet_pin_points,
)

WINDOW_MM = 25.4  # how far a Z route may swing beyond the box spanned by both ends
MAX_TARGET_POINTS = 24  # T-join candidates per target wire (nearest first)
MAX_GRID_STEPS = 200  # Z-route corner positions per axis
_DIRS: dict[str, tuple[int, int]] = {
    "left": (-1, 0),
    "right": (1, 0),
    "up": (0, -1),
    "down": (0, 1),
}


@dataclass(frozen=True)
class WireTarget:
    """A wire of the target net a route may end on (anywhere on it: T-join)."""

    uuid: str
    a: Point
    b: Point
    net: str


@dataclass
class Route:
    points: list[Point]
    junction_at: Point | None  # the T-join point on the target wire's interior
    cost: float = 0.0


def _fmt(p: Point) -> str:
    return f"({p[0]:g}, {p[1]:g})"


def _unit(a: Point, b: Point) -> tuple[int, int]:
    return (
        int((b[0] > a[0] + EPS_MM) - (b[0] < a[0] - EPS_MM)),
        int((b[1] > a[1] + EPS_MM) - (b[1] < a[1] - EPS_MM)),
    )


def _collinear_overlap(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Two axis-aligned segments share more than a point."""
    if abs(a[0] - b[0]) < EPS_MM and abs(c[0] - d[0]) < EPS_MM and abs(a[0] - c[0]) < EPS_MM:
        lo = max(min(a[1], b[1]), min(c[1], d[1]))
        hi = min(max(a[1], b[1]), max(c[1], d[1]))
        return hi - lo > EPS_MM
    if abs(a[1] - b[1]) < EPS_MM and abs(c[1] - d[1]) < EPS_MM and abs(a[1] - c[1]) < EPS_MM:
        lo = max(min(a[0], b[0]), min(c[0], d[0]))
        hi = min(max(a[0], b[0]), max(c[0], d[0]))
        return hi - lo > EPS_MM
    return False


def _on_line(a: Point, b: Point, line: tuple[Point, Point]) -> bool:
    """Segment a-b lies on the infinite axis-aligned line through `line`."""
    (c, d) = line
    if abs(c[0] - d[0]) < EPS_MM:  # vertical
        return abs(a[0] - c[0]) < EPS_MM and abs(b[0] - c[0]) < EPS_MM
    if abs(c[1] - d[1]) < EPS_MM:  # horizontal
        return abs(a[1] - c[1]) < EPS_MM and abs(b[1] - c[1]) < EPS_MM
    return False


def simplify(points: Sequence[Point]) -> list[Point]:
    """Drop repeated and collinear middle points; [] when the path reverses on itself."""
    pts = [points[0]]
    for p in points[1:]:
        if same_point(p, pts[-1]):
            continue
        if len(pts) >= 2:
            a, b = pts[-2], pts[-1]
            if (abs(a[0] - b[0]) < EPS_MM and abs(b[0] - p[0]) < EPS_MM) or (
                abs(a[1] - b[1]) < EPS_MM and abs(b[1] - p[1]) < EPS_MM
            ):
                if (b[0] - a[0]) * (p[0] - b[0]) + (b[1] - a[1]) * (p[1] - b[1]) < 0:
                    return []
                pts[-1] = p
                continue
        pts.append(p)
    return pts


_CELL_MM = 12.7  # bucket size of the obstacle index
_KEY = 100  # coordinates are keyed in 0.01 mm (EPS_MM) steps


def _key(v: float) -> int:
    return round(v * _KEY)


def _near(index: Mapping[int, list[Any]], v: float) -> Iterator[Any]:
    """Entries keyed within one key step of `v` (rounding at EPS_MM boundaries)."""
    k = _key(v)
    for kk in (k - 1, k, k + 1):
        yield from index.get(kk, ())


def _cells(box: Box) -> Iterator[tuple[int, int]]:
    for cx in range(math.floor(box.x0 / _CELL_MM), math.floor(box.x1 / _CELL_MM) + 1):
        for cy in range(math.floor(box.y0 / _CELL_MM), math.floor(box.y1 / _CELL_MM) + 1):
            yield cx, cy


class Obstacles:
    """One scene indexed for route checks against a set of allowed (joined) nets.

    Points are indexed by row (y -> x) and column (x -> y), wires by the line they lie
    on, bodies by 12.7 mm cells: a check looks only at items on a segment's line or
    in its cells, so routing stays fast on large sheets (review: route search inside
    the 30 s session call)."""

    def __init__(
        self,
        scene: Scene,
        netlist: Netlist,
        allowed_nets: set[str],
        item_nets: Mapping[str, str] | None = None,
    ) -> None:
        nets = item_nets or {}
        self.rows: dict[int, list[tuple[float, str]]] = {}  # foreign points by y
        self.cols: dict[int, list[tuple[float, str]]] = {}  # foreign points by x
        self.no_connects = list(scene.no_connects.values())
        self.h_wires: dict[int, list[tuple[float, float, bool, WireInfo]]] = {}
        self.v_wires: dict[int, list[tuple[float, float, bool, WireInfo]]] = {}
        self.bodies: dict[tuple[int, int], list[tuple[Box, str, str]]] = {}

        def point(p: Point, what: str) -> None:
            self.rows.setdefault(_key(p[1]), []).append((p[0], what))
            self.cols.setdefault(_key(p[0]), []).append((p[1], what))

        for pin in scene.all_pins():
            if netlist.net_of(pin.key) not in allowed_nets:
                point(pin.at, f"pin {pin.label} at {_fmt(pin.at)} (another net)")
        for w in scene.wires.values():
            foreign = nets.get(w.uuid) not in allowed_nets
            if foreign:
                for p in (w.start, w.end):
                    point(p, f"a wire end at {_fmt(p)} (another net)")
            if abs(w.start[1] - w.end[1]) < EPS_MM:
                lo, hi = sorted((w.start[0], w.end[0]))
                self.h_wires.setdefault(_key(w.start[1]), []).append((lo, hi, foreign, w))
            elif abs(w.start[0] - w.end[0]) < EPS_MM:
                lo, hi = sorted((w.start[1], w.end[1]))
                self.v_wires.setdefault(_key(w.start[0]), []).append((lo, hi, foreign, w))
        for lb in scene.labels.values():
            if nets.get(lb.uuid) not in allowed_nets:
                point(lb.at, f"label {lb.text} at {_fmt(lb.at)} (another net)")
        for uid, j in scene.junctions.items():
            if nets.get(uid) not in allowed_nets:
                point(j, f"a junction at {_fmt(j)} (another net)")
        for nc in self.no_connects:
            point(nc, f"a no-connect marker at {_fmt(nc)}")
        for sym in scene.symbols.values():
            for cell in _cells(sym.body):
                self.bodies.setdefault(cell, []).append(
                    (sym.body, sym.uuid, f"the body of {sym.ref}")
                )
        # every wire by its fixed coordinate, for counting perpendicular crossings
        self._v_all = sorted(
            (w.start[0], *sorted((w.start[1], w.end[1])))
            for w in scene.wires.values()
            if abs(w.start[0] - w.end[0]) < EPS_MM
        )
        self._h_all = sorted(
            (w.start[1], *sorted((w.start[0], w.end[0])))
            for w in scene.wires.values()
            if abs(w.start[1] - w.end[1]) < EPS_MM
        )
        self._v_x = [v[0] for v in self._v_all]
        self._h_y = [h[0] for h in self._h_all]
        # sheet symbols: the shared sheet-edge rule (#9), whatever the route's ends are
        self.sheet_pins = sheet_pin_points(scene)
        self.sheets: dict[tuple[int, int], list[SheetBox]] = {}
        for s in sheet_boxes(scene):
            for cell in _cells(s.box.grow(SHEET_EDGE_CLEARANCE_MM)):
                self.sheets.setdefault(cell, []).append(s)

    def crossings(self, a: Point, b: Point) -> int:
        """Wires the segment a-b crosses perpendicularly (interiors only)."""
        if abs(a[1] - b[1]) < EPS_MM:  # horizontal: vertical wires with x inside
            lo, hi = sorted((a[0], b[0]))
            i, j = (
                bisect.bisect_right(self._v_x, lo + EPS_MM),
                bisect.bisect_left(self._v_x, hi - EPS_MM),
            )
            return sum(1 for _x, y0, y1 in self._v_all[i:j] if y0 + EPS_MM < a[1] < y1 - EPS_MM)
        lo, hi = sorted((a[1], b[1]))
        i, j = (
            bisect.bisect_right(self._h_y, lo + EPS_MM),
            bisect.bisect_left(self._h_y, hi - EPS_MM),
        )
        return sum(1 for _y, x0, x1 in self._h_all[i:j] if x0 + EPS_MM < a[0] < x1 - EPS_MM)

    def _wires_through(self, p: Point) -> Iterator[tuple[bool, WireInfo, bool]]:
        """(foreign, wire, interior) for every wire `p` lies on."""
        for lo, hi, foreign, w in _near(self.h_wires, p[1]):
            if lo - EPS_MM <= p[0] <= hi + EPS_MM:
                yield foreign, w, lo + EPS_MM < p[0] < hi - EPS_MM
        for lo, hi, foreign, w in _near(self.v_wires, p[0]):
            if lo - EPS_MM <= p[1] <= hi + EPS_MM:
                yield foreign, w, lo + EPS_MM < p[1] < hi - EPS_MM

    def problems(
        self,
        points: Sequence[Point],
        own_symbols: tuple[str | None, str | None] = (None, None),
        first_only: bool = False,
        legacy_line: tuple[Point, Point] | None = None,
    ) -> list[str]:
        """Why a wire along `points` would be unsafe. `legacy_line`: the line of an
        existing wire that already broke the sheet-edge rule (#9); segments on that line
        keep it (they are the old wire, dragged), every other segment is checked."""
        pts = list(points)
        segs = list(itertools.pairwise(pts))
        if not segs:
            return ["the route has no length"]
        out: list[str] = []

        def add(msg: str) -> bool:
            out.append(msg)
            return first_only

        last = len(segs) - 1
        for i, (a, b) in enumerate(segs):
            horizontal = abs(a[1] - b[1]) < EPS_MM
            if not horizontal and abs(a[0] - b[0]) > EPS_MM:
                if add(f"segment {_fmt(a)}-{_fmt(b)} is diagonal"):
                    return out
                continue
            fixed, lo, hi = (
                (a[1], *sorted((a[0], b[0]))) if horizontal else (a[0], *sorted((a[1], b[1])))
            )
            for v, what in _near(self.rows if horizontal else self.cols, fixed):
                if lo - EPS_MM <= v <= hi + EPS_MM and add(f"touches {what}"):
                    return out
            for wlo, whi, _foreign, w in _near(self.h_wires if horizontal else self.v_wires, fixed):
                if min(hi, whi) - max(lo, wlo) > EPS_MM and add(
                    f"runs along the wire {_fmt(w.start)}-{_fmt(w.end)}"
                ):
                    return out
            seg_box = Box(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
            seen: set[str] = set()
            for cell in _cells(seg_box):
                for box, uid, what in self.bodies.get(cell, ()):
                    if uid in seen:
                        continue
                    seen.add(uid)
                    own = (i == 0 and uid == own_symbols[0]) or (
                        i == last and uid == own_symbols[1]
                    )
                    if not own and box.hit_by_segment(a, b) and add(f"crosses {what}"):
                        return out
            if legacy_line is not None and _on_line(a, b, legacy_line):
                continue
            near = {s.uuid: s for cell in _cells(seg_box) for s in self.sheets.get(cell, ())}
            for msg in sheet_edge_problems((a, b), list(near.values()), sheet_pins=self.sheet_pins):
                if add(msg):
                    return out
        for j, p in enumerate(pts):
            corner = 0 < j < len(pts) - 1
            for foreign, w, interior in self._wires_through(p):
                if foreign and interior:
                    msg = f"ends or turns on the wire {_fmt(w.start)}-{_fmt(w.end)} (another net)"
                elif not foreign and corner:  # a corner on a wire of our own nets: 4-way joint
                    msg = f"corner {_fmt(p)} on a wire"
                else:
                    continue
                if add(msg):
                    return out
        return sorted(set(out))


def route_problems(
    points: Sequence[Point],
    scene: Scene,
    netlist: Netlist,
    allowed_nets: set[str],
    *,
    item_nets: Mapping[str, str] | None = None,
    own_symbols: tuple[str | None, str | None] = (None, None),
    legacy_line: tuple[Point, Point] | None = None,
) -> list[str]:
    """Why a wire along `points` would be unsafe (empty = OK).

    `item_nets`: wire/label/junction uuid -> net on this sheet (unknown = foreign).
    Items of `allowed_nets` may be touched (contacts) and crossed; other items must not
    be touched anywhere. Corners never sit on a wire. `own_symbols`: the symbols whose
    pin the first / last point is: an outward first/last segment may touch their body
    box (it contains the pin)."""
    return Obstacles(scene, netlist, allowed_nets, item_nets).problems(
        points, own_symbols, legacy_line=legacy_line
    )


def needed_junctions(points: Sequence[Point], scene: Scene) -> list[Point]:
    """Points of a new route where three or more connections meet (wire ends, wire
    interiors count twice, pin ends) and no junction exists yet: KiCad needs a junction
    dot there (it drops junctions anywhere else, spike 2A Q5)."""
    pts = list(points)
    segs = list(itertools.pairwise(pts))
    candidates: list[Point] = list(pts)
    for w in scene.wires.values():
        candidates += [p for p in (w.start, w.end) if any(on_segment(p, a, b) for a, b in segs)]
    candidates += [p.at for p in scene.all_pins() if any(on_segment(p.at, a, b) for a, b in segs)]
    out: list[Point] = []
    all_wires = [(w.start, w.end) for w in scene.wires.values()] + segs
    for c in candidates:
        if any(same_point(c, o) for o in out) or any(
            same_point(c, j) for j in scene.junctions.values()
        ):
            continue
        arms = 0
        for a, b in all_wires:
            if same_point(c, a) or same_point(c, b):
                arms += 1
            elif in_segment_interior(c, a, b):
                arms += 2
        arms += sum(1 for p in scene.all_pins() if same_point(p.at, c))
        if arms >= 3:
            out.append(c)
    return out


def _grid_range(lo: float, hi: float) -> list[float]:
    """Grid values from `lo - WINDOW_MM` to `hi + WINDOW_MM`. Ends farther apart than
    MAX_GRID_STEPS steps keep the full window around both ends and sample the span
    between them evenly (detours on both sides stay possible)."""
    start, stop = snap(lo - WINDOW_MM), snap(hi + WINDOW_MM)
    n = round((stop - start) / GRID_MM)
    if n <= MAX_GRID_STEPS:
        return [round(start + i * GRID_MM, 4) for i in range(n + 1)]
    side = round(2 * WINDOW_MM / GRID_MM)
    low = [round(start + i * GRID_MM, 4) for i in range(side + 1)]
    high = [round(stop - i * GRID_MM, 4) for i in range(side + 1)]
    inner = max(MAX_GRID_STEPS - 2 * side, 2)
    step = max(1, -(-(n - 2 * side) // inner))  # ceil: at most `inner` samples
    middle = [round(start + i * GRID_MM, 4) for i in range(side, n - side, step)]
    return sorted(set(low + middle + high))


def _candidates(a: Point, b: Point) -> Iterable[list[Point]]:
    yield [a, b]
    yield [a, (b[0], a[1]), b]
    yield [a, (a[0], b[1]), b]
    for x in _grid_range(min(a[0], b[0]), max(a[0], b[0])):
        yield [a, (x, a[1]), (x, b[1]), b]
    for y in _grid_range(min(a[1], b[1]), max(a[1], b[1])):
        yield [a, (a[0], y), (b[0], y), b]


def _target_points(a: Point, t: WireTarget) -> list[Point]:
    """Up to MAX_TARGET_POINTS grid points on the target wire, nearest to `a` first
    (stepping outward from `a`'s projection onto the wire)."""
    vertical = abs(t.a[0] - t.b[0]) < EPS_MM
    fixed = t.a[0] if vertical else t.a[1]
    if not on_grid((fixed, fixed)):
        return []
    lo, hi = sorted((t.a[1], t.b[1]) if vertical else (t.a[0], t.b[0]))
    g_lo = math.ceil(lo / GRID_MM - 1e-6) * GRID_MM  # the on-grid part of the wire
    g_hi = math.floor(hi / GRID_MM + 1e-6) * GRID_MM
    if g_hi < g_lo - EPS_MM:
        return []
    centre = round(min(max(snap(a[1] if vertical else a[0]), g_lo), g_hi), 4)
    values: list[float] = []
    for i in range(4 * MAX_TARGET_POINTS):
        if len(values) >= MAX_TARGET_POINTS:
            break
        for v in (centre,) if i == 0 else (centre - i * GRID_MM, centre + i * GRID_MM):
            v = round(v, 4)
            if lo - EPS_MM <= v <= hi + EPS_MM and on_grid((v, v)):
                values.append(v)
    pts = [(fixed, v) if vertical else (v, fixed) for v in values[:MAX_TARGET_POINTS]]
    return sorted(pts, key=lambda p: abs(p[0] - a[0]) + abs(p[1] - a[1]))


def _length(pts: Sequence[Point]) -> float:
    return sum(abs(p[0] - q[0]) + abs(p[1] - q[1]) for p, q in itertools.pairwise(pts))


def route(
    a: PinEnd,
    b: PinEnd | WireTarget,
    scene: Scene,
    netlist: Netlist,
    *,
    allowed_nets: set[str],
    max_segments: int = 3,
    item_nets: Mapping[str, str] | None = None,
    deadline: float | None = None,
) -> Route | None:
    """Cheapest clean orthogonal route from pin end `a` to pin end `b` or onto wire `b`
    (T-join); None when no route of at most `max_segments` segments is clean.

    Each route leaves a pin outward or sideways (never into the body; sideways costs
    more) and enters a target pin the same way. Cost: segments, length, crossings of
    other wires, sideways starts and contacts with items of the joined nets."""
    if deadline is not None and time.monotonic() > deadline:
        return None  # the planning budget is spent: no more searching
    if not on_grid(a.at) or (isinstance(b, PinEnd) and not on_grid(b.at)):
        return None
    da = _DIRS[a.direction]
    own: tuple[str | None, str | None]
    if isinstance(b, PinEnd):
        ends = [b.at]
        db: tuple[int, int] | None = _DIRS[b.direction]
        own = (a.key[1], b.key[1])
    else:
        ends = _target_points(a.at, b)
        db = None
        own = (a.key[1], None)
    queue: list[tuple[float, list[Point]]] = []
    seen: set[tuple[Point, ...]] = set()
    for end in ends:
        if same_point(end, a.at):
            continue
        for cand in _candidates(a.at, end):
            pts = simplify(cand)
            if len(pts) < 2 or len(pts) - 1 > max_segments:
                continue
            key = tuple(pts)
            if key in seen:
                continue
            seen.add(key)
            first = _unit(pts[0], pts[1])
            if first == (-da[0], -da[1]):
                continue  # into a's own body
            cost = (len(pts) - 1) * 5.0 + _length(pts)
            if first != da:
                cost += 6.0  # sideways start
            back = _unit(pts[-1], pts[-2])
            if isinstance(b, PinEnd):
                assert db is not None
                if back == (-db[0], -db[1]):
                    continue
                if back != db:
                    cost += 6.0
            else:
                along = _unit(b.a, b.b)
                if back in (along, (-along[0], -along[1])):
                    continue  # arriving along the target wire would overlap it
            queue.append((cost, pts))
    queue.sort(key=lambda c: c[0])
    obstacles = Obstacles(scene, netlist, allowed_nets, item_nets)
    best: Route | None = None
    deadline_hit = False
    for n, (cost, pts) in enumerate(queue):
        if best is not None and cost >= best.cost:
            break  # crossings and junctions only add cost
        if deadline is not None and n % 64 == 0 and time.monotonic() > deadline:
            break  # planning budget spent: the best route so far (or none)
        if obstacles.problems(pts, own, first_only=True):
            continue
        segs = list(itertools.pairwise(pts))
        cost += 4.0 * sum(obstacles.crossings(p, q) for p, q in segs)
        if best is not None and cost >= best.cost:
            continue
        cost += 2.0 * len(needed_junctions(pts, scene))  # the expensive part: survivors only
        if deadline is not None and time.monotonic() > deadline:
            deadline_hit = True
        if best is None or cost < best.cost:
            junction = None
            if isinstance(b, WireTarget) and in_segment_interior(pts[-1], b.a, b.b):
                junction = pts[-1]
            best = Route(points=pts, junction_at=junction, cost=cost)
        if deadline_hit:
            break
    return best


def problems_summary(problems: list[str], limit: int = 6) -> str:
    return "; ".join(problems[:limit]) + (" ..." if len(problems) > limit else "")


def blocked_reasons(
    a: PinEnd,
    b: PinEnd,
    scene: Scene,
    netlist: Netlist,
    allowed_nets: set[str],
    item_nets: Mapping[str, str] | None = None,
) -> list[str]:
    """Why the simple routes (straight / L) between two pins fail, for error messages."""
    out: list[str] = []
    for cand in ([a.at, b.at], [a.at, (b.at[0], a.at[1]), b.at], [a.at, (a.at[0], b.at[1]), b.at]):
        pts = simplify(cand)
        if len(pts) >= 2:
            out += route_problems(
                pts,
                scene,
                netlist,
                allowed_nets,
                item_nets=item_nets,
                own_symbols=(a.key[1], b.key[1]),
            )
    return sorted(set(out))[:6]
