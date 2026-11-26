"""Pure reference-designator placement for `pcb_tidy_silk` (SP4 spec §7.2, P7 Task 5; no
kipy).

Every part's reference goes next to its courtyard (above, below, left, right at
`GAP_MM`, then the four corners), upright (0°; 90° for parts at 90° / 270°), on the
part's silk layer (`F.SilkS`, `B.SilkS` mirrored for bottom parts), in one uniform size.
A spot is free when the text box overlaps no pad (any part, + `PAD_MARGIN_MM`), no silk
graphic, no other visible silk text, no reference placed before it, and lies inside the
board outline; among the free spots one that also keeps off other parts' courtyards (a
reference under a part body is unreadable on the assembled board) is preferred. Parts go
in natural ref order. A part with no free spot is `unresolved` (the tool hides it with a
report line).

Text box estimate: `len(text) x height` wide (the M14 box, `metrics._text_poly`: stroke
font advance ≈ the size) plus the line width, `height + line` high - a superset of the
plan's `len x 0.9 x height + line`, so M14 never sees an overlap the planner thought free.
Deterministic."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from copperchat.board.geom import point_in_polygon, polygons_overlap, rect_poly
from copperchat.board.metrics import _graphic_segments, _pad_poly, _text_poly
from copperchat.board.model import BoardModel, Part, Point
from copperchat.board.staging import natural_key

__all__ = [
    "GAP_MM",
    "PAD_MARGIN_MM",
    "RefPlacement",
    "candidate_spots",
    "plan_refs",
    "text_size",
]

GAP_MM = 0.2  # text box to courtyard
CELL_MM = 2.0  # obstacle bucket size
PAD_MARGIN_MM = 0.1  # text box to any pad
_SILK = {"top": "F.SilkS", "bottom": "B.SilkS"}

Box = tuple[float, float, float, float]  # x0, y0, x1, y1


@dataclass(frozen=True)
class RefPlacement:
    ref: str
    at: Point  # text centre
    rot: float  # degrees, board frame
    layer: str  # F.SilkS / B.SilkS (bottom: mirrored)
    candidate: int = 0  # index into `candidate_spots` (P7: stage 2 excludes failed ones)


def _bbox(pts: Sequence[Point]) -> Box:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _part_box(p: Part) -> Box:
    if len(p.courtyard) >= 3:
        return _bbox(p.courtyard)
    pads = [c for q in p.pads for c in _pad_poly(q)]
    if pads:
        return _bbox(pads)
    return p.x - 0.5, p.y - 0.5, p.x + 0.5, p.y + 0.5


def _rot(p: Part) -> float:
    return 90.0 if round(p.rot) % 180 == 90 else 0.0


def text_size(text: str, height: float, line: float, rot: float) -> tuple[float, float]:
    """(width, height) of the text box on the board (90°: swapped)."""
    w = max(len(text), 1) * height + line
    h = height + line
    return (h, w) if rot == 90.0 else (w, h)


def candidate_spots(p: Part, text: str, *, height: float, line: float) -> list[Point]:
    """The 8 candidate centres: above, below, left, right of the courtyard box at GAP_MM,
    then the corners (top-left, top-right, bottom-left, bottom-right)."""
    x0, y0, x1, y1 = _part_box(p)
    w, h = text_size(text, height, line, _rot(p))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    up, down = y0 - GAP_MM - h / 2, y1 + GAP_MM + h / 2
    left, right = x0 - GAP_MM - w / 2, x1 + GAP_MM + w / 2
    return [
        (cx, up),
        (cx, down),
        (left, cy),
        (right, cy),
        (left, up),
        (right, up),
        (left, down),
        (right, down),
    ]


def _box(at: Point, w: float, h: float) -> Box:
    return at[0] - w / 2, at[1] - h / 2, at[0] + w / 2, at[1] + h / 2


def _poly(b: Box) -> list[Point]:
    return [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]


def _boxes_overlap(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _seg_hits_box(a: Point, b: Point, box: Box, r: float) -> bool:
    """Segment a-b (half width r) touches the axis-aligned `box` (Liang-Barsky clip)."""
    x0, y0, x1, y1 = box[0] - r, box[1] - r, box[2] + r, box[3] + r
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


class _Index[T]:
    """Items bucketed by bounding box (`CELL_MM` cells): `near(box)` returns the items
    whose box may overlap `box` (each once)."""

    def __init__(self) -> None:
        self.items: list[tuple[Box, T]] = []
        self.cells: dict[tuple[int, int], list[int]] = {}

    def _keys(self, b: Box) -> list[tuple[int, int]]:
        x0, y0 = math.floor(b[0] / CELL_MM), math.floor(b[1] / CELL_MM)
        x1, y1 = math.floor(b[2] / CELL_MM), math.floor(b[3] / CELL_MM)
        return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]

    def add(self, b: Box, item: T) -> None:
        self.items.append((b, item))
        for k in self._keys(b):
            self.cells.setdefault(k, []).append(len(self.items) - 1)

    def near(self, b: Box) -> list[tuple[Box, T]]:
        seen: set[int] = set()
        for k in self._keys(b):
            seen.update(self.cells.get(k, ()))
        return [self.items[i] for i in sorted(seen)]


class _Board:
    """The fixed obstacles per silk layer, bucketed."""

    def __init__(self, model: BoardModel, moving: set[str]) -> None:
        self.pads: _Index[list[Point]] = _Index()
        for p in model.parts:
            for q in p.pads:
                poly = _pad_poly(q)
                self.pads.add(_bbox(poly), poly)
        self.courts: _Index[str] = _Index()
        for p in model.parts:
            if len(p.courtyard) >= 3:
                self.courts.add(_bbox(p.courtyard), p.ref)
        self.segs: dict[str, _Index[tuple[Point, Point, float]]] = {
            "F.SilkS": _Index(),
            "B.SilkS": _Index(),
        }
        graphics = [*model.graphics, *(g for p in model.parts for g in p.silk)]
        for g in graphics:
            if g.layer in self.segs:
                r = g.width / 2
                for a, b in _graphic_segments(g.kind, g.points):
                    x0, x1 = sorted((a[0], b[0]))
                    y0, y1 = sorted((a[1], b[1]))
                    self.segs[g.layer].add((x0 - r, y0 - r, x1 + r, y1 + r), (a, b, r))
        self.texts: dict[str, _Index[None]] = {"F.SilkS": _Index(), "B.SilkS": _Index()}
        others = [(p.ref, t) for p in model.parts for t in p.texts]
        others += [("", t) for t in model.texts]
        for ref, t in others:
            if not t.visible or t.layer not in self.texts or not t.text.strip():
                continue
            if t.kind == "reference" and ref in moving:
                continue  # placed by this plan
            self.texts[t.layer].add(_bbox(_text_poly(t)), None)
        self.placed: dict[str, _Index[None]] = {"F.SilkS": _Index(), "B.SilkS": _Index()}
        o = model.outline
        if o.kind == "rect":
            self.outline: list[Point] | None = rect_poly(o.x, o.y, o.w, o.h, o.corner_radius)
        elif o.kind == "other" and len(o.polygon) >= 3:
            self.outline = list(o.polygon)
        else:
            self.outline = None

    def free(self, box: Box, layer: str) -> bool:
        if self.outline is not None and not all(
            point_in_polygon(c, self.outline) for c in _poly(box)
        ):
            return False
        grown = (
            box[0] - PAD_MARGIN_MM,
            box[1] - PAD_MARGIN_MM,
            box[2] + PAD_MARGIN_MM,
            box[3] + PAD_MARGIN_MM,
        )
        gp = _poly(grown)
        for bb, poly in self.pads.near(grown):
            if _boxes_overlap(grown, bb) and polygons_overlap(gp, poly):
                return False
        if any(_seg_hits_box(a, b, box, r) for _, (a, b, r) in self.segs[layer].near(box)):
            return False
        if any(_boxes_overlap(box, t) for t, _ in self.texts[layer].near(box)):
            return False
        return not any(_boxes_overlap(box, t) for t, _ in self.placed[layer].near(box))

    def place(self, box: Box, layer: str) -> None:
        self.placed[layer].add(box, None)

    def off_courtyards(self, box: Box, ref: str) -> bool:
        return not any(r != ref and _boxes_overlap(box, b) for b, r in self.courts.near(box))


def _reference(p: Part) -> str | None:
    t = next((t for t in p.texts if t.kind == "reference"), None)
    return None if t is None else (t.text or p.ref)


def plan_refs(
    model: BoardModel,
    *,
    height: float,
    line: float,
    skip: frozenset[str] = frozenset(),
    exclude: Mapping[str, frozenset[int]] | None = None,
) -> tuple[tuple[RefPlacement, ...], tuple[str, ...]]:
    """(placements, unresolved refs) - module docstring. `skip`: refs left as they are
    (their visible reference stays an obstacle); `exclude`: candidate indices a ref may
    not use (the spots KiCad's DRC rejected)."""
    exclude = exclude or {}
    parts = sorted(
        (p for p in model.parts if p.ref not in skip and _reference(p) is not None),
        key=lambda p: natural_key(p.ref),
    )
    board = _Board(model, {p.ref for p in parts})
    out: list[RefPlacement] = []
    unresolved: list[str] = []
    for p in parts:
        text = _reference(p) or p.ref
        rot = _rot(p)
        layer = _SILK[p.side]
        w, h = text_size(text, height, line, rot)
        banned = exclude.get(p.ref, frozenset())
        best: tuple[int, Point, Box] | None = None
        for i, at in enumerate(candidate_spots(p, text, height=height, line=line)):
            if i in banned:
                continue
            box = _box(at, w, h)
            if not board.free(box, layer):
                continue
            if board.off_courtyards(box, p.ref):
                best = (i, at, box)
                break
            if best is None:
                best = (i, at, box)
        if best is None:
            unresolved.append(p.ref)
            continue
        i, at, box = best
        board.place(box, layer)
        out.append(RefPlacement(p.ref, at, rot, layer, i))
    return tuple(out), tuple(unresolved)
