"""The legaliser (B-C11, spec §9.2): removes courtyard overlaps and outline / keepout
violations with small shifts; locked and non-movable parts never move. Pure, deterministic.

Overlap = two courtyard boxes on the same side closer than the board's courtyard clearance
(`courtyard_clearance` rule, 0 when unset). Each round: clamp every movable part into the
outline minus `edge_margin`, push it out of keepouts, then push every overlapping pair
apart along the axis of the smaller penetration (both movable: half each). Shifts are whole
0.05 mm grid steps rounded up, so positions stay on the grid and gaps never shrink. A part
still in conflict after the rounds goes to the nearest free spot around its start (0.25 mm
rings, within `max_shift`, else 20 mm).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from copperchat.board.geom import polygon_inside, polygons_overlap, rect_poly
from copperchat.board.model import BoardModel, Part, Point
from copperchat.board.placer.layout import (
    GRID_MM,
    Box,
    Move,
    apply_moves,
    box_of,
    boxes_hit,
    snap,
    snap_up,
)
from copperchat.board.staging import natural_key

MAX_ROUNDS = 200
FALLBACK_STEP_MM = 0.25  # ring pitch of the nearest-free-spot fallback
FALLBACK_RADIUS_MM = 20.0  # its reach without a max_shift
DEFAULT_EDGE_MARGIN_MM = 0.3
_EPS = 1e-9


@dataclass(frozen=True)
class DoesNotFit:
    needed_mm2: float
    available_mm2: float
    refs: tuple[str, ...]


def rule(model: BoardModel, name: str) -> float | None:
    for k, v in model.rules:
        if k == name:
            return v
    return None


def edge_margin(model: BoardModel) -> float:
    """Copper-to-edge clearance of the board (`copper_edge_clearance`), 0.3 mm when unset."""
    v = rule(model, "copper_edge_clearance")
    return v if v is not None and v > 0 else DEFAULT_EDGE_MARGIN_MM


def courtyard_clearance(model: BoardModel) -> float:
    v = rule(model, "courtyard_clearance")
    return max(v or 0.0, 0.0)


def _area(b: Box) -> float:
    return max(b[2] - b[0], 0.0) * max(b[3] - b[1], 0.0)


def _intersect(a: Box, b: Box) -> Box:
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def _bbox(pts: Sequence[Point]) -> Box:
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


class Outline:
    """The outline minus a margin: the inner box plus an exact polygon test for rounded or
    polygon outlines."""

    def __init__(self, model: BoardModel, margin: float) -> None:
        o = model.outline
        self.kind = o.kind
        self.margin = margin
        if o.kind == "none":
            self.inner: Box = (-math.inf, -math.inf, math.inf, math.inf)
            self.poly: list[Point] | None = None
            return
        self.inner = (o.x + margin, o.y + margin, o.x + o.w - margin, o.y + o.h - margin)
        if o.kind == "rect":
            r = o.corner_radius
            self.poly = rect_poly(o.x, o.y, o.w, o.h, r) if r > 0 else None
            self._r = r
        else:
            self.poly = list(o.polygon) if len(o.polygon) >= 3 else None
            self._r = math.inf
        b = (o.x, o.y, o.x + o.w, o.y + o.h)
        r = min(self._r, o.w / 2, o.h / 2) if self.poly is not None else 0.0
        # boxes inside one of these need no polygon test (rounded rect only)
        self._cross = (
            (b[0] + r + margin, b[1] + margin, b[2] - r - margin, b[3] - margin),
            (b[0] + margin, b[1] + r + margin, b[2] - margin, b[3] - r - margin),
        )

    def contains(self, b: Box) -> bool:
        i = self.inner
        if b[0] < i[0] - _EPS or b[1] < i[1] - _EPS or b[2] > i[2] + _EPS or b[3] > i[3] + _EPS:
            return False
        if self.poly is None:
            return True
        if self.kind == "rect" and any(
            c[0] - _EPS <= b[0]
            and c[1] - _EPS <= b[1]
            and b[2] <= c[2] + _EPS
            and b[3] <= c[3] + _EPS
            for c in self._cross
        ):
            return True
        m = self.margin
        return polygon_inside(
            rect_poly(b[0] - m, b[1] - m, b[2] - b[0] + 2 * m, b[3] - b[1] + 2 * m), self.poly
        )

    def area(self) -> float:
        return _area(self.inner) if self.kind != "none" else math.inf

    def centre(self) -> Point:
        i = self.inner
        return ((i[0] + i[2]) / 2, (i[1] + i[3]) / 2)


def on_board(model: BoardModel, p: Part) -> bool:
    """The part takes part in legalising: its courtyard box touches the outline's box
    (parts waiting in the staging area right of the outline do not)."""
    o = model.outline
    if o.kind == "none":
        return True
    b = box_of(p)
    return b[0] < o.x + o.w and o.x < b[2] and b[1] < o.y + o.h and o.y < b[3]


def keepout_polys(model: BoardModel) -> list[tuple[Point, ...]]:
    """Footprint keepout rule areas of the board."""
    return [
        z.outline
        for z in model.zones
        if z.rule_area and "footprints" in z.keepout and len(z.outline) >= 3
    ]


def _hits_keepout(b: Box, keepouts: Sequence[tuple[Point, ...]]) -> tuple[Point, ...] | None:
    poly = rect_poly(b[0], b[1], b[2] - b[0], b[3] - b[1])
    for k in keepouts:
        kb = _bbox(k)
        if (
            b[0] < kb[2]
            and kb[0] < b[2]
            and b[1] < kb[3]
            and kb[1] < b[3]
            and polygons_overlap(poly, k)
        ):
            return k
    return None


def _spiral(radius: float) -> list[tuple[float, float]]:
    """Offsets on the 0.25 mm grid within `radius`, nearest first (ties: y, x)."""
    n = int(radius / FALLBACK_STEP_MM)
    pts = [
        (i * FALLBACK_STEP_MM, j * FALLBACK_STEP_MM)
        for i in range(-n, n + 1)
        for j in range(-n, n + 1)
        if math.hypot(i, j) * FALLBACK_STEP_MM <= radius + 1e-9
    ]
    pts.sort(key=lambda p: (math.hypot(*p), p[1], p[0]))
    return pts


def _step(v: float) -> float:
    """A shift of at least |v|, in whole grid steps, sign kept."""
    return math.copysign(snap_up(abs(v)), v) if abs(v) > _EPS else 0.0


def legalize(
    model: BoardModel,
    moves: Sequence[Move],
    *,
    movable: frozenset[str],
    max_shift: float | None,
    keepouts: Sequence[tuple[Point, ...]],
    edge_margin: float,
) -> tuple[Move, ...] | DoesNotFit:
    """Legal positions for every movable part (natural ref order; position from `moves`,
    else the board): no courtyard overlap with any part on the board, inside the outline
    minus `edge_margin`, outside `keepouts`, on the 0.05 mm grid. A part that would shift
    more than `max_shift` from its start, or overlaps left after `MAX_ROUNDS` →
    `DoesNotFit`."""
    m = apply_moves(model, moves)
    moved = {mv.ref for mv in moves}
    parts = [p for p in m.parts if p.ref in moved or on_board(m, p)]
    parts.sort(key=lambda p: natural_key(p.ref))
    mov = {p.ref for p in parts if p.ref in movable and not p.locked}
    sep = courtyard_clearance(m)
    outline = Outline(m, edge_margin)
    # box relative to the origin; positions start on the grid
    rel: dict[str, Box] = {}
    pos: dict[str, list[float]] = {}
    for p in parts:
        b = box_of(p)
        x, y = (snap(p.x), snap(p.y)) if p.ref in mov else (p.x, p.y)
        rel[p.ref] = (b[0] - p.x, b[1] - p.y, b[2] - p.x, b[3] - p.y)
        pos[p.ref] = [x, y]
    start = {r: tuple(pos[r]) for r in mov}
    side = {p.ref: p.side for p in parts}
    rot = {p.ref: p.rot for p in parts}

    def bx(r: str) -> Box:
        b, (x, y) = rel[r], pos[r]
        return (b[0] + x, b[1] + y, b[2] + x, b[3] + y)

    def clamp(r: str) -> bool:
        b, i = bx(r), outline.inner
        dx = (i[0] - b[0] if b[0] < i[0] else 0.0) + (i[2] - b[2] if b[2] > i[2] else 0.0)
        dy = (i[1] - b[1] if b[1] < i[1] else 0.0) + (i[3] - b[3] if b[3] > i[3] else 0.0)
        if b[2] - b[0] > i[2] - i[0] or b[3] - b[1] > i[3] - i[1]:
            return False  # cannot fit at all; reported below
        if abs(dx) > _EPS or abs(dy) > _EPS:
            pos[r][0] += _step(dx)
            pos[r][1] += _step(dy)
            return True
        if outline.poly is not None and not outline.contains(b):
            cx, cy = outline.centre()
            mx, my = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            pos[r][0] += math.copysign(GRID_MM, cx - mx) if abs(cx - mx) > _EPS else 0.0
            pos[r][1] += math.copysign(GRID_MM, cy - my) if abs(cy - my) > _EPS else 0.0
            return True
        return False

    def unkeep(r: str) -> bool:
        b = bx(r)
        k = _hits_keepout(b, keepouts)
        if k is None:
            return False
        kb = _bbox(k)
        opts = [
            (b[2] - kb[0] + sep, (-1.0, 0.0)),
            (kb[2] - b[0] + sep, (1.0, 0.0)),
            (b[3] - kb[1] + sep, (0.0, -1.0)),
            (kb[3] - b[1] + sep, (0.0, 1.0)),
        ]
        d, (ux, uy) = min(opts, key=lambda o: o[0])
        pos[r][0] += ux * snap_up(d)
        pos[r][1] += uy * snap_up(d)
        return True

    def push(a: str, b: str) -> bool:
        ba, bb = bx(a), bx(b)
        if side[a] != side[b] or not boxes_hit(ba, bb, sep):
            return False
        ma, mb = a in mov, b in mov
        if not ma and not mb:
            return False
        px = min(ba[2] - bb[0], bb[2] - ba[0]) + sep
        py = min(ba[3] - bb[1], bb[3] - ba[1]) + sep
        ca = ((ba[0] + ba[2]) / 2, (ba[1] + ba[3]) / 2)
        cb = ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)
        axis = 0 if px <= py else 1
        pen = px if axis == 0 else py
        sign = 1.0 if (ca[axis], a) > (cb[axis], b) else -1.0  # a moves this way
        if ma and mb:
            half = snap_up(pen / 2)
            pos[a][axis] += sign * half
            pos[b][axis] -= sign * (snap_up(pen - half) if pen - half > _EPS else 0.0)
        elif ma:
            pos[a][axis] += sign * snap_up(pen)
        else:
            pos[b][axis] -= sign * snap_up(pen)
        return True

    order = [p.ref for p in parts]
    for _ in range(MAX_ROUNDS):
        changed = False
        for r in order:
            if r in mov:
                changed |= clamp(r)
                changed |= unkeep(r)
        swept = sorted(order, key=lambda r: (bx(r)[0], natural_key(r)))
        for i, ra in enumerate(swept):
            box_a = bx(ra)
            for rb in swept[i + 1 :]:
                if bx(rb)[0] >= box_a[2] + sep:
                    break
                if push(ra, rb):
                    changed = True
                    box_a = bx(ra)
        if not changed:
            break

    def conflict(r: str) -> bool:
        b = bx(r)
        if not outline.contains(b) or _hits_keepout(b, keepouts) is not None:
            return True
        return any(o != r and side[o] == side[r] and boxes_hit(b, bx(o), sep) for o in order)

    # the push-apart can get stuck (a part squeezed between fixed ones along one axis):
    # move each part still in conflict to the nearest free spot around its start
    radius = max_shift if max_shift is not None else FALLBACK_RADIUS_MM
    for r in sorted(mov, key=natural_key):
        if not conflict(r):
            continue
        sx, sy = start[r]
        for dx, dy in _spiral(radius):
            pos[r] = [round(sx + dx, 6), round(sy + dy, 6)]
            if not conflict(r):
                break
        else:
            pos[r] = [sx, sy]

    bad: set[str] = set()
    for r in mov:
        b = bx(r)
        if not outline.contains(b) or _hits_keepout(b, keepouts) is not None:
            bad.add(r)
        if max_shift is not None and math.dist(start[r], pos[r]) > max_shift + 1e-6:
            bad.add(r)
    for i, ra in enumerate(order):
        for rb in order[i + 1 :]:
            if (ra in mov or rb in mov) and side[ra] == side[rb] and boxes_hit(bx(ra), bx(rb), sep):
                bad.update(r for r in (ra, rb) if r in mov)
    if bad:
        needed = sum(_area(bx(r)) for r in mov)
        fixed = sum(_area(_intersect(bx(r), outline.inner)) for r in order if r not in mov)
        return DoesNotFit(
            needed_mm2=round(needed, 3),
            available_mm2=round(max(outline.area() - fixed, 0.0), 3),
            refs=tuple(sorted(bad, key=natural_key)),
        )
    return tuple(
        Move(r, round(pos[r][0], 6), round(pos[r][1], 6), rot[r], side[r])
        for r in sorted(mov, key=natural_key)
    )
