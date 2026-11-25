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
    )
