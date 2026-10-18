"""Free space on a sheet for a new symbol (E4 Task 1, port of spike 2D `sch_find_space`).

A candidate position is free when the part's boxes (body + 1.27 mm, one box per field,
the room in front of its own pins) touch no obstacle of the scene: symbol bodies
(+ 2 mm) and fields, sheet symbols, labels, wires, junctions, no-connects, and the
corridor in front of every unconnected pin (5.08 x 2.54 mm, room for a stub, label or
power symbol, spike 2D v2.1). `sch_place_symbol`'s own overlap check is a subset of
this, so every candidate found here is accepted by a following placement.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from copperchat.kicad_tools.edit.geometry import (
    EPS_MM,
    GRID_MM,
    Box,
    Direction,
    PinEnd,
    Point,
    snap,
)
from copperchat.kicad_tools.edit.scene import LabelInfo, Scene

STEP_MM = 2 * GRID_MM  # search grid (pins of stock symbols sit on 2.54 mm)
MAX_RING = 60  # rings of the spiral (60 x 2.54 mm ~ 15 cm around the anchor)
PAGE_MARGIN_MM = 10.0  # inside the drawing sheet frame
CORRIDOR_LEN_MM = 5.08
CORRIDOR_HALF_MM = 1.27
BODY_CLEARANCE_MM = 1.27
LABEL_CHAR_MM = 0.8  # label text at 1.27 mm: ~0.6 em per character + shape
_DIR: dict[str, tuple[int, int]] = {"right": (1, 0), "left": (-1, 0), "up": (0, -1), "down": (0, 1)}
_CELL_MM = 25.4


def direction_vector(direction: Direction | str) -> tuple[int, int]:
    return _DIR[direction]


def pin_corridor(
    pin: PinEnd, length: float = CORRIDOR_LEN_MM, half: float = CORRIDOR_HALF_MM
) -> Box:
    """The room in front of a pin end (outward), `length` long and 2 x `half` wide."""
    dx, dy = _DIR[pin.direction]
    tip = (pin.at[0] + dx * length, pin.at[1] + dy * length)
    return Box.around([pin.at, tip]).grow(half)


def label_cross(lb: LabelInfo) -> list[Box]:
    """A conservative cross around a label anchor: the text may run in any direction."""
    reach = LABEL_CHAR_MM * max(len(lb.text), 1) + GRID_MM
    x, y = lb.at
    return [
        Box(x - reach, y - GRID_MM, x + reach, y + GRID_MM),
        Box(x - GRID_MM, y - reach, x + GRID_MM, y + reach),
    ]


def named_obstacles(scene: Scene, exclude: frozenset[str] = frozenset()) -> list[tuple[str, Box]]:
    """What a placed part's body and fields must not overlap (`sch_place_symbol`), each
    with a name for error messages; the same boxes as `Scene.obstacles` plus labels,
    wires, junctions and no-connects."""
    out: list[tuple[str, Box]] = []
    for s in scene.symbols.values():
        if s.uuid in exclude:
            continue
        out.append((f"symbol {s.ref}", s.body.grow(2.0)))
        out += [(f"a field of {s.ref}", b) for b in s.fields]
    out += [("a sheet symbol", b) for b in scene.sheets.values()]
    for lb in scene.labels.values():
        out += [(f"label {lb.text}", b) for b in label_cross(lb)]
    for w in scene.wires.values():
        out.append((f"wire {w.start}-{w.end}", Box.around([w.start, w.end])))
    out += [
        (f"junction at {p}", Box.around([p, p]).grow(GRID_MM / 2)) for p in scene.junctions.values()
    ]
    out += [
        (f"no-connect at {p}", Box.around([p, p]).grow(GRID_MM / 2))
        for p in scene.no_connects.values()
    ]
    return out


def placement_obstacles(scene: Scene) -> list[Box]:
    return [b for _, b in named_obstacles(scene)]


def _key(p: Point) -> tuple[int, int]:
    return (round(p[0] * 100), round(p[1] * 100))  # 0.01 mm cells (EPS_MM)


def unconnected_pins(scene: Scene) -> list[PinEnd]:
    """Pin ends with nothing else at them (no wire, label, pin, junction, no-connect).
    Indexed by point and wire line, not a scan per pin (a full sheet has thousands)."""
    count: dict[tuple[int, int], int] = {}
    pins = scene.all_pins()
    points = [p.at for p in pins]
    points += [q for w in scene.wires.values() for q in (w.start, w.end)]
    points += [lb.at for lb in scene.labels.values()]
    points += [*scene.junctions.values(), *scene.no_connects.values()]
    for q in points:
        count[_key(q)] = count.get(_key(q), 0) + 1
    rows: dict[int, list[tuple[float, float]]] = {}  # y key -> x spans of horizontal wires
    cols: dict[int, list[tuple[float, float]]] = {}  # x key -> y spans of vertical wires
    for w in scene.wires.values():
        (x0, y0), (x1, y1) = w.start, w.end
        if abs(y0 - y1) < EPS_MM:
            rows.setdefault(round(y0 * 100), []).append((min(x0, x1), max(x0, x1)))
        elif abs(x0 - x1) < EPS_MM:
            cols.setdefault(round(x0 * 100), []).append((min(y0, y1), max(y0, y1)))
    out: list[PinEnd] = []
    for pin in pins:
        x, y = pin.at
        if count.get(_key(pin.at), 0) > 1:
            continue
        inside = any(a + EPS_MM < x < b - EPS_MM for a, b in rows.get(round(y * 100), ()))
        inside = inside or any(a + EPS_MM < y < b - EPS_MM for a, b in cols.get(round(x * 100), ()))
        if not inside:
            out.append(pin)
    return out


def search_obstacles(scene: Scene, keep_free: Iterable[Box] = ()) -> list[Box]:
    """Obstacles of the free-space search: placement obstacles + free pin corridors."""
    return [
        *placement_obstacles(scene),
        *(pin_corridor(p) for p in unconnected_pins(scene)),
        *keep_free,
    ]


class _Index:
    """Obstacle boxes bucketed in 25.4 mm cells (a full sheet has hundreds of boxes and
    the spiral tests thousands of positions)."""

    def __init__(self, boxes: Sequence[Box]) -> None:
        self.boxes = list(boxes)
        self.cells: dict[tuple[int, int], list[int]] = {}
        for i, b in enumerate(self.boxes):
            for cell in self._cells(b):
                self.cells.setdefault(cell, []).append(i)

    @staticmethod
    def _cells(b: Box) -> Iterable[tuple[int, int]]:
        for cx in range(math.floor(b.x0 / _CELL_MM), math.floor(b.x1 / _CELL_MM) + 1):
            for cy in range(math.floor(b.y0 / _CELL_MM), math.floor(b.y1 / _CELL_MM) + 1):
                yield (cx, cy)

    def hits(self, box: Box) -> bool:
        seen: set[int] = set()
        for cell in self._cells(box):
            for i in self.cells.get(cell, ()):
                if i in seen:
                    continue
                seen.add(i)
                if box.overlaps(self.boxes[i]):
                    return True
        return False


def shift(box: Box, dx: float, dy: float) -> Box:
    return Box(box.x0 + dx, box.y0 + dy, box.x1 + dx, box.y1 + dy)


@dataclass(frozen=True)
class Candidate:
    at: Point
    box: Box  # body + pins + fields of the part at `at`
    distance: float


def find_space(
    scene: Scene,
    shape: Sequence[Box],
    near: Point,
    *,
    direction: Direction | None = None,
    keep_free: Sequence[Box] = (),
    outline: Sequence[Box] | None = None,
    limit: int = 3,
    max_ring: int = MAX_RING,
) -> list[Candidate]:
    """Up to `limit` free grid positions for a part, nearest to `near` first.

    `shape`: the part's boxes relative to its origin (body + clearance, fields, its own
    pin corridors); `outline`: the boxes reported as the candidate's `box` (default
    `shape`). `direction`: only positions on the side a pin points to. Candidates are at
    least two search steps apart, so they are real alternatives.
    """
    if not shape:
        return []
    index = _Index(search_obstacles(scene, keep_free))
    outline = list(outline or shape)
    vec = _DIR[direction] if direction else None
    ox, oy = snap(near[0]), snap(near[1])
    min_x = min(b.x0 for b in shape)
    min_y = min(b.y0 for b in shape)
    hull = Box(min_x, min_y, max(b.x1 for b in shape), max(b.y1 for b in shape))
    out_hull = Box(
        min(b.x0 for b in outline),
        min(b.y0 for b in outline),
        max(b.x1 for b in outline),
        max(b.y1 for b in outline),
    )
    # The union box first: a free hull means every box is free (most positions on an
    # empty area); otherwise the boxes one by one.
    found: list[Candidate] = []
    picks: list[Candidate] = []
    grown = False
    for r in range(0, max_ring + 1):
        # Ring r holds no position nearer than r steps: once `limit` spaced picks are all
        # nearer than that, no later ring can improve them (nearest first, Euclidean).
        if grown:
            picks = _spaced(found, limit)
            grown = False
        if len(picks) >= limit and r * STEP_MM > picks[-1].distance + 1e-6:
            break
        for dx, dy in _ring(r):
            if vec and dx * vec[0] + dy * vec[1] <= 0:
                continue  # stay on the side the pin points to
            at = (round(ox + dx * STEP_MM, 4), round(oy + dy * STEP_MM, 4))
            if at[0] + min_x < PAGE_MARGIN_MM or at[1] + min_y < PAGE_MARGIN_MM:
                continue
            if index.hits(shift(hull, *at)) and any(index.hits(shift(b, *at)) for b in shape):
                continue
            found.append(Candidate(at, shift(out_hull, *at), round(math.dist(at, near), 3)))
            grown = True
    return _spaced(found, limit)


def _spaced(found: list[Candidate], limit: int) -> list[Candidate]:
    """Nearest first, each at least two search steps from the ones before."""
    out: list[Candidate] = []
    for c in sorted(found, key=lambda c: (c.distance, c.at[1], c.at[0])):
        if all(math.dist(c.at, o.at) >= 2 * STEP_MM - 1e-6 for o in out):
            out.append(c)
            if len(out) >= limit:
                break
    return out


def _ring(r: int) -> Iterable[tuple[int, int]]:
    """The grid offsets at Chebyshev distance `r` (the spiral's r-th ring)."""
    if r == 0:
        yield (0, 0)
        return
    for d in range(-r, r + 1):
        yield (d, -r)
        yield (d, r)
    for d in range(-r + 1, r):
        yield (-r, d)
        yield (r, d)
