"""Placement score (B-C11, spec §9.2): the placement metrics M1, M8, M9, M10 of P3a plus
the total airwire and a routability estimate. Pure."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from copperchat.board.boardmd import BoardDoc
from copperchat.board.classify import Classification
from copperchat.board.geom import octi_dist
from copperchat.board.metrics import compute_metrics, connections
from copperchat.board.model import BoardModel
from copperchat.board.placer.layout import box_of
from copperchat.board.placer.legalize import edge_margin, on_board

CELL_MM = 5.0
# Routability: at most this many pads in any 5 x 5 mm cell (spec call 21; tuned in P4
# Task 5 on sp4_synth, see the plan index "As built (P4)").
MAX_CELL_PINS = 30
_ONLY = frozenset({"M1", "M8", "M9", "M10"})


@dataclass(frozen=True)
class PlacementScore:
    m1_violations: int
    m8_worst_mm: float | None
    m9_reviews: int
    m10_worst_ratio: float | None
    airwire_mm: float
    max_cell_pins: int
    routability_ok: bool
    # R2: how much of the usable area the placement spans (`spread`); reported, not a metric
    spread: float = 0.0


def airwire_mm(model: BoardModel, cls: Classification) -> float:
    """Σ over every net except ground nets (planes join them) of its octilinear MST."""
    nets = sorted({q.net for p in model.parts for q in p.pads if q.net})
    total = 0.0
    for net in nets:
        if cls.net_kind.get(net) == "ground":
            continue
        total += sum(octi_dist((a.x, a.y), (b.x, b.y)) for a, b in connections(model, net))
    return round(total, 3)


def max_cell_pins(model: BoardModel) -> int:
    """Most pads in one 5 x 5 mm cell (cells from the outline's corner; pads outside the
    outline's box are not counted; without an outline every pad, cells from (0, 0))."""
    o = model.outline
    x0, y0 = (o.x, o.y) if o.kind != "none" else (0.0, 0.0)
    cells: Counter[tuple[int, int]] = Counter()
    for p in model.parts:
        for q in p.pads:
            if o.kind != "none" and not (o.x <= q.x <= o.x + o.w and o.y <= q.y <= o.y + o.h):
                continue
            cells[(int((q.x - x0) // CELL_MM), int((q.y - y0) // CELL_MM))] += 1
    return max(cells.values(), default=0)


_SPREAD_SKIP = frozenset({"mounting_hole", "fiducial"})


def spread(model: BoardModel, cls: Classification) -> float:
    """min(union-bbox width / usable width, height / usable height) of the courtyards of
    the parts on the board (mounting holes and fiducials not counted: they sit in the
    corners anyway); usable = the outline's box inset by the edge margin. 0.0 without an
    outline or parts."""
    o = model.outline
    if o.kind == "none":
        return 0.0
    m = edge_margin(model)
    uw, uh = o.w - 2 * m, o.h - 2 * m
    boxes = [
        box_of(p)
        for p in model.parts
        if cls.part_class.get(p.ref) not in _SPREAD_SKIP
        and len(p.courtyard) + len(p.pads) > 0
        and on_board(model, p)
    ]
    if not boxes or uw <= 0 or uh <= 0:
        return 0.0
    x0 = max(min(b[0] for b in boxes), o.x + m)
    y0 = max(min(b[1] for b in boxes), o.y + m)
    x1 = min(max(b[2] for b in boxes), o.x + o.w - m)
    y1 = min(max(b[3] for b in boxes), o.y + o.h - m)
    return round(min(max(x1 - x0, 0.0) / uw, max(y1 - y0, 0.0) / uh, 1.0), 4)


def score_placement(model: BoardModel, cls: Classification, doc: BoardDoc) -> PlacementScore:
    report = compute_metrics(model, cls, doc, drc=None, parity=None, preset=None, only=_ONLY)
    by_id = {m.id: m for m in report.metrics}
    m1, m8, m9, m10 = by_id["M1"], by_id["M8"], by_id["M9"], by_id["M10"]
    cell = max_cell_pins(model)
    return PlacementScore(
        m1_violations=int(m1.value or 0),
        m8_worst_mm=None if m8.value is None else float(m8.value),
        m9_reviews=int(m9.value or 0),
        m10_worst_ratio=None if m10.value is None else float(m10.value),
        airwire_mm=airwire_mm(model, cls),
        max_cell_pins=cell,
        routability_ok=cell <= MAX_CELL_PINS,
        spread=spread(model, cls),
    )
