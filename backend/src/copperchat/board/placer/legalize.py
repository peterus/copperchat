"""The legaliser (B-C11, spec §9.2): removes courtyard overlaps and outline / keepout
violations with small shifts; locked and non-movable parts never move. Pure, deterministic.

Overlap = two courtyard boxes on the same side closer than the board's courtyard clearance
(`courtyard_clearance` rule, 0 when unset). Each round: clamp every movable part into the
outline minus `edge_margin`, push it out of keepouts, then push every overlapping pair
apart along the axis of the smaller penetration (both movable: half each). A legal part
keeps its exact position; a part that has to move is snapped onto the 0.05 mm grid first and
shifted in whole grid steps rounded up, so gaps never shrink. A part still in conflict after
the rounds goes to the nearest free spot around its start (0.25 mm rings, within
`max_shift`, else 20 mm). Both phases have deterministic work budgets (`push_rounds`,
`FALLBACK_TRIES`): what is left in conflict is reported as `DoesNotFit`.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from collections.abc import Iterator, Sequence
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
FALLBACK_CELL_MM = 10.0  # bucket size of the fallback's box index
INSIDE_CELL_MM = 2.0  # cell size of the polygon outline's memoised inside test
FALLBACK_TRIES = 200_000  # spots the fallback may test per call (deterministic budget)
PUSH_PAIR_BUDGET = 100_000  # push rounds = min(MAX_ROUNDS, max(20, this // parts))
SNAP_SLACK_MM = GRID_MM * math.sqrt(2) / 2  # snapping onto the grid may add this to a shift
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
        self._cells: dict[tuple[int, int], str] = {}
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
        if self.kind != "rect":
            verdict = self._cells_verdict(b)
            if verdict is not None:
                return verdict
        return self._exact(b)

    def _exact(self, b: Box) -> bool:
        assert self.poly is not None
        m = self.margin
        return polygon_inside(
            rect_poly(b[0] - m, b[1] - m, b[2] - b[0] + 2 * m, b[3] - b[1] + 2 * m), self.poly
        )

    def _cell(self, i: int, j: int) -> str:
        """'in' (cell fully inside the outline minus margin), 'out' (the cell grown by the
        margin misses the outline polygon) or 'band' — the exact tests once per cell."""
        state = self._cells.get((i, j))
        if state is None:
            assert self.poly is not None
            c, m = INSIDE_CELL_MM, self.margin
            box = (i * c, j * c, (i + 1) * c, (j + 1) * c)
            if self._exact(box):
                state = "in"
            elif not polygons_overlap(
                rect_poly(box[0] - m, box[1] - m, c + 2 * m, c + 2 * m), self.poly
            ):
                state = "out"
            else:
                state = "band"
            self._cells[(i, j)] = state
        return state

    def _cells_verdict(self, b: Box) -> bool | None:
        """Polygon outlines: True when `b` lies in cells that are all inside, False when one
        of its cells lies outside, None (exact test needed) otherwise."""
        c = INSIDE_CELL_MM
        verdict: bool | None = True
        for i in range(math.floor(b[0] / c), math.floor(b[2] / c) + 1):
            for j in range(math.floor(b[1] / c), math.floor(b[3] / c) + 1):
                state = self._cell(i, j)
                if state == "out":
                    return False
                if state == "band":
                    verdict = None
        return verdict

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


class _BoxIndex:
    """Refs bucketed by their boxes (uniform grid) for near-neighbour queries."""

    def __init__(self, cell: float) -> None:
        self.cell = cell
        self.cells: dict[tuple[int, int], set[str]] = defaultdict(set)
        self.where: dict[str, list[tuple[int, int]]] = {}

    def _keys(self, b: Box, grow: float = 0.0) -> list[tuple[int, int]]:
        c = self.cell
        return [
            (i, j)
            for i in range(math.floor((b[0] - grow) / c), math.floor((b[2] + grow) / c) + 1)
            for j in range(math.floor((b[1] - grow) / c), math.floor((b[3] + grow) / c) + 1)
        ]

    def add(self, r: str, b: Box) -> None:
        keys = self._keys(b)
        self.where[r] = keys
        for k in keys:
            self.cells[k].add(r)

    def remove(self, r: str) -> None:
        for k in self.where.pop(r, []):
            self.cells[k].discard(r)

    def near(self, b: Box, grow: float) -> set[str]:
        out: set[str] = set()
        for k in self._keys(b, grow):
            out |= self.cells.get(k, set())
        return out


def _rings(radius: float) -> Iterator[tuple[float, float]]:
    """Offsets on the 0.25 mm grid within `radius`, nearest first (ties y, x), generated
    lazily: Chebyshev ring k only holds points at least k steps away, so after ring k every
    buffered point closer than k + 1 steps is final."""
    n = int(radius / FALLBACK_STEP_MM)
    heap: list[tuple[float, int, int]] = []
    for k in range(n + 1):
        for i in range(-k, k + 1):
            for j in range(-k, k + 1):
                if (
                    max(abs(i), abs(j)) == k
                    and math.hypot(i, j) * FALLBACK_STEP_MM <= radius + 1e-9
                ):
                    heapq.heappush(heap, (math.hypot(i, j), j, i))
        while heap and (heap[0][0] < k + 1 or k == n):
            _, j, i = heapq.heappop(heap)
            yield (i * FALLBACK_STEP_MM, j * FALLBACK_STEP_MM)


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
    minus `edge_margin`, outside `keepouts`; a part that moves lands on the 0.05 mm grid, a
    legal one keeps its exact position. A part that would shift more than `max_shift` (+ the
    grid snap, `SNAP_SLACK_MM`) from its start, or conflicts left after the budgets →
    `DoesNotFit`."""
    m = apply_moves(model, moves)
    moved = {mv.ref for mv in moves}
    parts = [p for p in m.parts if p.ref in moved or on_board(m, p)]
    parts.sort(key=lambda p: natural_key(p.ref))
    mov = {p.ref for p in parts if p.ref in movable and not p.locked}
    sep = courtyard_clearance(m)
    outline = Outline(m, edge_margin)
    # box relative to the origin; a part keeps its exact position until it has to move,
    # then it goes onto the 0.05 mm grid first (`touch`): a legal part is never rewritten
    rel: dict[str, Box] = {}
    pos: dict[str, list[float]] = {}
    for p in parts:
        b = box_of(p)
        rel[p.ref] = (b[0] - p.x, b[1] - p.y, b[2] - p.x, b[3] - p.y)
        pos[p.ref] = [p.x, p.y]
    start = {r: tuple(pos[r]) for r in mov}
    touched: set[str] = set()

    def touch(r: str) -> None:
        if r not in touched:
            touched.add(r)
            pos[r] = [snap(pos[r][0]), snap(pos[r][1])]

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
            touch(r)
            b = bx(r)
            dx = (i[0] - b[0] if b[0] < i[0] else 0.0) + (i[2] - b[2] if b[2] > i[2] else 0.0)
            dy = (i[1] - b[1] if b[1] < i[1] else 0.0) + (i[3] - b[3] if b[3] > i[3] else 0.0)
            pos[r][0] += _step(dx)
            pos[r][1] += _step(dy)
            return True
        if outline.poly is not None and not outline.contains(b):
            touch(r)
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
        touch(r)
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
        for r in (a, b):
            if r in mov:
                touch(r)
        ba, bb = bx(a), bx(b)
        px = min(ba[2] - bb[0], bb[2] - ba[0]) + sep
        py = min(ba[3] - bb[1], bb[3] - ba[1]) + sep
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
    push_rounds = min(MAX_ROUNDS, max(20, PUSH_PAIR_BUDGET // max(len(order), 1)))
    for _ in range(push_rounds):
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

    index = _BoxIndex(FALLBACK_CELL_MM)
    for r in order:
        index.add(r, bx(r))

    def conflict(r: str) -> bool:
        b = bx(r)  # cheapest test first: neighbours, keepouts, then the outline
        if any(
            o != r and side[o] == side[r] and boxes_hit(b, bx(o), sep) for o in index.near(b, sep)
        ):
            return True
        return _hits_keepout(b, keepouts) is not None or not outline.contains(b)

    # the push-apart can get stuck (a part squeezed between fixed ones along one axis):
    # move each part still in conflict to the nearest free spot around its start, within
    # FALLBACK_TRIES spots overall (then the rest is reported as does_not_fit)
    radius = max_shift if max_shift is not None else FALLBACK_RADIUS_MM
    tries = FALLBACK_TRIES
    for r in sorted(mov, key=natural_key):
        if tries <= 0:
            break
        if not conflict(r):
            continue
        index.remove(r)
        sx, sy = snap(start[r][0]), snap(start[r][1])
        for dx, dy in _rings(radius):
            tries -= 1
            if tries < 0:
                break
            cand = (round(sx + dx, 6), round(sy + dy, 6))
            if math.dist(start[r], cand) > radius + SNAP_SLACK_MM:
                continue
            pos[r] = [cand[0], cand[1]]
            if not conflict(r):
                touched.add(r)
                break
        else:
            pos[r] = [sx, sy]
        index.add(r, bx(r))

    bad: set[str] = set()
    for r in mov:
        b = bx(r)
        if not outline.contains(b) or _hits_keepout(b, keepouts) is not None:
            bad.add(r)
        # the grid snap of a moved part may add up to SNAP_SLACK_MM to its shift
        if max_shift is not None and math.dist(start[r], pos[r]) > max_shift + SNAP_SLACK_MM + 1e-6:
            bad.add(r)
    final = _BoxIndex(FALLBACK_CELL_MM)
    for r in order:
        final.add(r, bx(r))
    for ra in order:
        for rb in final.near(bx(ra), sep):
            pair = ra < rb and (ra in mov or rb in mov) and side[ra] == side[rb]
            if pair and boxes_hit(bx(ra), bx(rb), sep):
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
