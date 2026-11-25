"""Copper selection and removal shared by the placement tools (P4, routed-part rule, spec
§7.2 "Moves on a routed board") and `pcb_unroute` (P5b).

`select_copper` picks tracks, arcs and vias by seed (nets, a region, the pads of parts) and
extends every seed to its whole **connection component**: the same-net graph of tracks,
arcs and vias joined where they meet (shared end points, a track end on another track of
the same layer, a track end inside a via, a via on a track). Pads are the terminals of a
component: copper is never joined *through* a pad, so moving one part removes the copper
of its own connections only and never the routes between other parts; nothing removed
leaves a dangling end behind (what stays was connected to the removed copper only through
a pad, which stays). Zones are never selected. Pure except `unroute_stage` (one board commit)."""

from __future__ import annotations

import itertools
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from kipy.proto.common.types import KIID

from copperchat.board.geom import _in_pad, _pad_on_layer
from copperchat.board.model import ArcTrack, BoardModel, Pad, Point, Rect, Track, Via
from copperchat.board.staging import natural_key
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.board.engine import BoardStage, BoardStageContext
from copperchat.kicad_tools.board.read import BOARD_READ_TIMEOUT_S

__all__ = ["CopperSelection", "attached", "select_copper", "unroute_stage"]

JOIN_TOL_MM = 0.001  # end points this close are joined
_CELL_MM = 2.0


@dataclass(frozen=True)
class CopperSelection:
    track_kiids: frozenset[str]  # tracks and arcs
    via_kiids: frozenset[str]
    nets: tuple[str, ...]  # nets of the selected items, sorted
    locked: int  # components skipped because they hold a locked item (include_locked=False)

    @property
    def empty(self) -> bool:
        return not self.track_kiids and not self.via_kiids


# -- geometry ------------------------------------------------------------------------------

_Seg = Track | ArcTrack


def _ends(t: _Seg) -> tuple[Point, Point]:
    return (t.start, t.end)


def _polyline(t: _Seg) -> list[Point]:
    return [t.start, t.mid, t.end] if isinstance(t, ArcTrack) else [t.start, t.end]


def _seg_dist(p: Point, a: Point, b: Point) -> float:
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    n = dx * dx + dy * dy
    if n <= 0:
        return math.dist(p, a)
    u = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / n))
    return math.dist(p, (ax + u * dx, ay + u * dy))


def _circle(t: ArcTrack) -> tuple[Point, float] | None:
    """Centre and radius of the circle through the arc's start, mid and end (None when
    the three points are collinear)."""
    (ax, ay), (bx, by), (cx, cy) = t.start, t.mid, t.end
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    return (ux, uy), math.dist((ux, uy), t.start)


def _in_span(t: ArcTrack, c: Point, p: Point) -> bool:
    """The direction c→p lies within the arc's angular span (start → mid → end)."""

    def ang(q: Point) -> float:
        return math.atan2(q[1] - c[1], q[0] - c[0])

    tau = 2 * math.pi
    s, m, e, a = ang(t.start), ang(t.mid), ang(t.end), ang(p)
    sweep = (e - s) % tau
    if (m - s) % tau <= sweep:  # counter-clockwise start → end
        return (a - s) % tau <= sweep
    return (a - e) % tau <= (s - e) % tau  # clockwise: ccw from end to start


def _on_track(p: Point, t: _Seg, reach: float = 0.0) -> bool:
    """`p` within half the width (+ `reach`, e.g. a via radius) of the track's centre line;
    an arc is tested against its real circle, not its chords."""
    lim = t.width / 2 + reach + JOIN_TOL_MM
    if isinstance(t, ArcTrack):
        circ = _circle(t)
        if circ is not None:
            c, r = circ
            if abs(math.dist(c, p) - r) <= lim and _in_span(t, c, p):
                return True
            return math.dist(p, t.start) <= lim or math.dist(p, t.end) <= lim
    pts = _polyline(t)
    return any(_seg_dist(p, a, b) <= lim for a, b in itertools.pairwise(pts))


def _seg_hits_rect(a: Point, b: Point, r: tuple[float, float, float, float]) -> bool:
    """Segment a-b intersects the box r (x0, y0, x1, y1) — Liang-Barsky clipping."""
    x0, y0, x1, y1 = r
    dx, dy = b[0] - a[0], b[1] - a[1]
    lo, hi = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            lo = max(lo, t)
        else:
            hi = min(hi, t)
        if lo > hi:
            return False
    return True


def _in_region(item: _Seg | Via, region: Rect) -> bool:
    if isinstance(item, Via):
        r = item.diameter / 2
        x, y = item.at
        cx = min(max(x, region.x), region.x + region.w)
        cy = min(max(y, region.y), region.y + region.h)
        return math.dist((x, y), (cx, cy)) <= r
    g = item.width / 2
    box = (region.x - g, region.y - g, region.x + region.w + g, region.y + region.h + g)
    pts = _polyline(item)
    return any(_seg_hits_rect(a, b, box) for a, b in itertools.pairwise(pts))


def _copper(p: Pad) -> bool:
    return any(layer.endswith(".Cu") for layer in p.layers)


def _touches(item: _Seg | Via, pad: Pad) -> bool:
    """A track end on one of the pad's layers inside the pad, or a via centre in it."""
    if isinstance(item, Via):
        return _copper(pad) and _in_pad(item.at, pad)
    return _pad_on_layer(pad, item.layer) and any(_in_pad(e, pad) for e in _ends(item))


class _Buckets:
    """A uniform grid of item indices: a via or an arc by its bounding box, a straight
    track by the cells its band touches (proportional to its length, not its bbox area)."""

    def __init__(self) -> None:
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)

    @staticmethod
    def _range(x0: float, y0: float, x1: float, y1: float) -> Iterable[tuple[int, int]]:
        for i in range(math.floor(x0 / _CELL_MM), math.floor(x1 / _CELL_MM) + 1):
            for j in range(math.floor(y0 / _CELL_MM), math.floor(y1 / _CELL_MM) + 1):
                yield (i, j)

    def add(self, k: int, box: tuple[float, float, float, float]) -> None:
        for c in self._range(*box):
            self.cells[c].append(k)

    def add_segment(self, k: int, a: Point, b: Point, g: float) -> None:
        """Every cell within `g` of segment a-b: per cell column, the y range of the segment
        over the column's x interval grown by g, grown by g again."""
        (xa, ya), (xb, yb) = sorted((a, b))
        dx = xb - xa
        for i in range(math.floor((xa - g) / _CELL_MM), math.floor((xb + g) / _CELL_MM) + 1):
            lo = max(xa, i * _CELL_MM - g)
            hi = min(xb, (i + 1) * _CELL_MM + g)
            if lo > hi:
                continue
            if dx <= 0:
                y0, y1 = min(ya, yb), max(ya, yb)
            else:
                yl = ya + (yb - ya) * (lo - xa) / dx
                yh = ya + (yb - ya) * (hi - xa) / dx
                y0, y1 = min(yl, yh), max(yl, yh)
            for j in range(math.floor((y0 - g) / _CELL_MM), math.floor((y1 + g) / _CELL_MM) + 1):
                self.cells[(i, j)].append(k)

    def add_item(self, k: int, item: _Seg | Via) -> None:
        if isinstance(item, Track):
            self.add_segment(k, item.start, item.end, item.width / 2)
        else:
            self.add(k, _bbox(item))

    def near(self, p: Point, r: float) -> set[int]:
        out: set[int] = set()
        for c in self._range(p[0] - r, p[1] - r, p[0] + r, p[1] + r):
            out.update(self.cells.get(c, ()))
        return out


def _grid(items: Sequence[_Seg | Via]) -> _Buckets:
    grid = _Buckets()
    for k, it in enumerate(items):
        grid.add_item(k, it)
    return grid


def _bbox(item: _Seg | Via) -> tuple[float, float, float, float]:
    if isinstance(item, Via):
        r = item.diameter / 2
        return (item.at[0] - r, item.at[1] - r, item.at[0] + r, item.at[1] + r)
    pts = _polyline(item)
    if isinstance(item, ArcTrack) and (circ := _circle(item)) is not None:
        (cx, cy), r = circ  # the arc's bulge: the circle's extreme points within its span
        for q in ((cx + r, cy), (cx - r, cy), (cx, cy + r), (cx, cy - r)):
            if _in_span(item, (cx, cy), q):
                pts.append(q)
    g = item.width / 2
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs) - g, min(ys) - g, max(xs) + g, max(ys) + g)


def _pads_of(model: BoardModel, refs: Iterable[str]) -> list[Pad]:
    wanted = set(refs)
    return [q for p in model.parts if p.ref in wanted for q in p.pads]


# -- components ----------------------------------------------------------------------------


def _components(items: list[_Seg | Via]) -> list[int]:
    """Union-find root per item (all of one net)."""
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    grid = _grid(items)
    for k, it in enumerate(items):
        if isinstance(it, Via):
            # a via on a same-net track (not only at one of its ends) joins it too
            vr = it.diameter / 2
            for j in grid.near(it.at, vr + JOIN_TOL_MM):
                other = items[j]
                if not isinstance(other, Via) and _on_track(it.at, other, vr):
                    union(k, j)
            continue
        for e in _ends(it):
            for j in grid.near(e, JOIN_TOL_MM):
                if j == k:
                    continue
                other = items[j]
                if isinstance(other, Via):
                    if math.dist(e, other.at) <= other.diameter / 2 + JOIN_TOL_MM:
                        union(k, j)
                elif other.layer == it.layer and _on_track(e, other):
                    union(k, j)
    return [find(k) for k in range(len(items))]


def attached(model: BoardModel, refs: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """ref → the nets (sorted) of the copper attached to its pads: a track or arc end inside
    one of its pads on a pad layer, or a via whose centre lies in a copper pad. Refs
    without attached copper are absent."""
    wanted = set(refs)
    items: list[_Seg | Via] = [*model.tracks, *model.arcs, *model.vias]
    grid = _grid(items)
    out: dict[str, set[str]] = defaultdict(set)
    for p in model.parts:
        if p.ref not in wanted:
            continue
        for q in p.pads:
            for k in grid.near((q.x, q.y), math.hypot(q.w, q.h) / 2 + JOIN_TOL_MM):
                if _touches(items[k], q):
                    out[p.ref].add(items[k].net)
    return {r: tuple(sorted(n, key=natural_key)) for r, n in sorted(out.items())}


def select_copper(
    model: BoardModel,
    *,
    nets: Sequence[str] = (),
    region: Rect | None = None,
    pads_of: Sequence[str] = (),
    include_locked: bool = False,
) -> CopperSelection:
    """Tracks, arcs and vias seeded by `nets` (all of their copper), `region` (items
    intersecting it) and `pads_of` (items touching a pad of those parts), each extended to
    its whole connection component (module docstring). With `include_locked=False` a
    component holding a locked item is skipped entirely and counted in `locked`."""
    every: list[_Seg | Via] = [*model.tracks, *model.arcs, *model.vias]
    net_seeds = set(nets)
    seeded: set[int] = {k for k, it in enumerate(every) if it.net in net_seeds}
    if region is not None:
        seeded.update(k for k, it in enumerate(every) if _in_region(it, region))
    pads = _pads_of(model, pads_of)
    if pads:
        grid = _grid(every)  # pads only see nearby copper (like `attached`)
        for q in pads:
            for k in grid.near((q.x, q.y), math.hypot(q.w, q.h) / 2 + JOIN_TOL_MM):
                if _touches(every[k], q):
                    seeded.add(k)
    by_net: dict[str, list[int]] = defaultdict(list)
    for k, it in enumerate(every):
        by_net[it.net].append(k)
    seed_nets = {every[k].net for k in seeded}
    tracks: set[str] = set()
    vias: set[str] = set()
    picked_nets: set[str] = set()
    skipped = 0
    for net in sorted(seed_nets):
        idx = by_net[net]
        items = [every[k] for k in idx]
        seeds = [i for i, k in enumerate(idx) if k in seeded]
        roots = _components(items)
        members: dict[int, list[int]] = defaultdict(list)
        for k, r in enumerate(roots):
            members[r].append(k)
        for r in sorted({roots[k] for k in seeds}):
            comp = [items[k] for k in members[r]]
            if not include_locked and any(it.locked for it in comp):
                skipped += 1
                continue
            for it in comp:
                (vias if isinstance(it, Via) else tracks).add(it.kiid)
            picked_nets.add(net)
    return CopperSelection(
        frozenset(tracks),
        frozenset(vias),
        tuple(sorted(picked_nets, key=natural_key)),
        skipped,
    )


def _kiids(ids: Iterable[str]) -> list[KIID]:
    out = []
    for value in sorted(ids):
        k = KIID()
        k.value = value
        out.append(k)
    return out


def _remove(h: KicadHandle, ids: frozenset[str]) -> None:
    if ids:
        h.board().remove_items_by_id(_kiids(ids))


def unroute_stage(sel: CopperSelection) -> BoardStage:
    """One board commit that removes the selected tracks, arcs and vias."""
    ids = sel.track_kiids | sel.via_kiids

    async def run(sctx: BoardStageContext) -> None:
        await sctx.call(lambda h: _remove(h, ids), timeout=BOARD_READ_TIMEOUT_S)

    return BoardStage("unroute", run)
