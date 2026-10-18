"""Orthogonal wire router with T-joins (E5, spec §5.2).

Port of spike 2D `geo.route` / `path_problems`, extended after 2D failure mode 4: a
route may end on a wire of the target net (T-join, junction), and items of the nets
being joined are contacts, not obstacles. A route never touches a connection point of
another net (pin end, wire end, label, junction), never runs along or turns on a wire
of another net (all of these would connect it, spike 2A Q3), never enters a symbol or
sheet body, and has at most `max_segments` segments. Every 1-3 segment orthogonal path
is either straight, an L or a Z (H-V-H / V-H-V), so the candidates below enumerate all
of them within a window around both ends.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from copperchat.kicad_tools.access import Netlist
from copperchat.kicad_tools.edit.geometry import (
    EPS_MM,
    GRID_MM,
    PinEnd,
    Point,
    in_segment_interior,
    on_grid,
    on_segment,
    same_point,
    snap,
)
from copperchat.kicad_tools.edit.scene import Scene

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


def _crosses(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Perpendicular crossing of both interiors (no connection without a junction)."""
    if abs(a[0] - b[0]) < EPS_MM and abs(c[1] - d[1]) < EPS_MM:
        x, y = a[0], c[1]
        return (
            min(c[0], d[0]) + EPS_MM < x < max(c[0], d[0]) - EPS_MM
            and min(a[1], b[1]) + EPS_MM < y < max(a[1], b[1]) - EPS_MM
        )
    if abs(a[1] - b[1]) < EPS_MM and abs(c[0] - d[0]) < EPS_MM:
        return _crosses(c, d, a, b)
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


def route_problems(
    points: Sequence[Point],
    scene: Scene,
    netlist: Netlist,
    allowed_nets: set[str],
    *,
    item_nets: Mapping[str, str] | None = None,
    own_symbols: tuple[str | None, str | None] = (None, None),
) -> list[str]:
    """Why a wire along `points` would be unsafe (empty = OK).

    `item_nets`: wire/label/junction uuid -> net on this sheet (unknown = foreign).
    Items of `allowed_nets` may be touched (contacts) and crossed; other items must not
    be touched anywhere. Corners never sit on a wire. `own_symbols`: the symbols whose
    pin the first / last point is: an outward first/last segment may touch their body
    box (it contains the pin)."""
    nets = item_nets or {}
    pts = list(points)
    segs = list(itertools.pairwise(pts))
    if not segs:
        return ["the route has no length"]
    ends = (pts[0], pts[-1])
    problems: list[str] = []

    def touches(p: Point) -> bool:
        return any(on_segment(p, a, b) for a, b in segs)

    for a, b in segs:
        if abs(a[0] - b[0]) > EPS_MM and abs(a[1] - b[1]) > EPS_MM:
            problems.append(f"segment {_fmt(a)}-{_fmt(b)} is diagonal")
    for pin in scene.all_pins():
        if netlist.net_of(pin.key) in allowed_nets:
            continue
        if touches(pin.at):
            problems.append(f"touches pin {pin.label} at {_fmt(pin.at)} (another net)")
    for w in scene.wires.values():
        foreign = nets.get(w.uuid) not in allowed_nets
        for a, b in segs:
            if _collinear_overlap(a, b, w.start, w.end):
                problems.append(f"runs along the wire {_fmt(w.start)}-{_fmt(w.end)}")
                break
        if foreign:
            for p in (w.start, w.end):
                if touches(p):
                    problems.append(f"touches a wire end at {_fmt(p)} (another net)")
            for p in pts:
                if in_segment_interior(p, w.start, w.end):
                    problems.append(
                        f"ends or turns on the wire {_fmt(w.start)}-{_fmt(w.end)} (another net)"
                    )
        else:
            for p in pts[1:-1]:  # a corner on a wire of our own nets: ugly 4-way joint
                if on_segment(p, w.start, w.end):
                    problems.append(f"corner {_fmt(p)} on a wire")
    for lb in scene.labels.values():
        if nets.get(lb.uuid) not in allowed_nets and touches(lb.at):
            problems.append(f"touches label {lb.text} at {_fmt(lb.at)} (another net)")
    for uid, j in scene.junctions.items():
        if nets.get(uid) not in allowed_nets and touches(j):
            problems.append(f"touches a junction at {_fmt(j)} (another net)")
    for nc in scene.no_connects.values():
        if touches(nc):
            problems.append(f"touches a no-connect marker at {_fmt(nc)}")
    last = len(segs) - 1
    for i, (a, b) in enumerate(segs):
        for sym in scene.symbols.values():
            own = (i == 0 and sym.uuid == own_symbols[0]) or (
                i == last and sym.uuid == own_symbols[1]
            )
            if not own and sym.body.hit_by_segment(a, b):
                problems.append(f"crosses the body of {sym.ref}")
        for box in scene.sheets.values():
            if box.grow(0.2).hit_by_segment(a, b):  # sheet pins sit on the sheet edge
                problems.append("crosses a sheet symbol")
    for p in ends:
        for nc in scene.no_connects.values():
            if same_point(p, nc):
                problems.append(f"ends on a no-connect marker at {_fmt(p)}")
    return sorted(set(problems))


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
    """Grid values from `lo - WINDOW_MM` to `hi + WINDOW_MM`, at most MAX_GRID_STEPS of
    them (ends farther apart than a sheet is wide do not get every detour)."""
    start, stop = snap(lo - WINDOW_MM), snap(hi + WINDOW_MM)
    n = min(round((stop - start) / GRID_MM), MAX_GRID_STEPS)
    return [round(start + i * GRID_MM, 4) for i in range(n + 1)]


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
    centre = min(max(snap(a[1] if vertical else a[0]), lo), hi)
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
) -> Route | None:
    """Cheapest clean orthogonal route from pin end `a` to pin end `b` or onto wire `b`
    (T-join); None when no route of at most `max_segments` segments is clean.

    Each route leaves a pin outward or sideways (never into the body; sideways costs
    more) and enters a target pin the same way. Cost: segments, length, crossings of
    other wires, sideways starts and contacts with items of the joined nets."""
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
    best: Route | None = None
    for cost, pts in queue:
        if best is not None and cost >= best.cost:
            break  # crossings and junctions only add cost
        if route_problems(pts, scene, netlist, allowed_nets, item_nets=item_nets, own_symbols=own):
            continue
        segs = list(itertools.pairwise(pts))
        cost += 4.0 * sum(
            _crosses(p, q, w.start, w.end) for p, q in segs for w in scene.wires.values()
        )
        cost += 2.0 * len(needed_junctions(pts, scene))
        if best is None or cost < best.cost:
            junction = None
            if isinstance(b, WireTarget) and in_segment_interior(pts[-1], b.a, b.b):
                junction = pts[-1]
            best = Route(points=pts, junction_at=junction, cost=cost)
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
