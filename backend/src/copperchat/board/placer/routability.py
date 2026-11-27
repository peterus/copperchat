"""Routing room of a placement (R2b, owner feedback 2026-11-27): the shared definitions of
the relaxed placer (track pitch, net weights, channel widths) and three pure checks that
`pcb_place_blocks` reports.

* **congestion** - RUDY (Rectangular Uniform wire DensitY): every non-ground connection
  spreads its octilinear length uniformly over its bounding box (at least one pitch wide);
  per `CONG_CELL_MM` cell the copper it needs (length x pitch) over the copper room (cell
  area minus pad area, on `ROUTING_LAYERS` layers). > 1.0: more tracks than fit.
* **largest hole** - the largest empty rectangle inside the placement's bounding box (free
  space collected in one place instead of shared out).
* **block gaps** - per connected block pair: the gap between their parts and the channel
  their shared nets need.

Pure, deterministic, no I/O."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from copperchat.board.classify import BlockProposal, Classification
from copperchat.board.geom import octi_dist
from copperchat.board.metrics import connections
from copperchat.board.model import BoardModel
from copperchat.board.placer.layout import Box, box_of
from copperchat.board.placer.legalize import edge_margin, on_board
from copperchat.board.staging import natural_key

DEFAULT_PITCH_MM = 0.4  # 0.2 mm track + 0.2 mm clearance without any net class
SUPPLY_WEIGHT = 0.25  # supply nets reach every block through pours: a weak tie
CHANNEL_BASE_MM = 1.0  # one via + one track always fit between two units
CHANNEL_MAX_MM = 4.0
CONG_CELL_MM = 2.5
ROUTING_LAYERS = 2  # both outer layers; inner planes are not routing room
HOLE_GRID_MM = 0.5
_SKIP = frozenset({"mounting_hole", "fiducial"})
_EPS = 1e-9


# ---- shared definitions ---------------------------------------------------------------------


def net_pitch(model: BoardModel, net: str) -> float:
    """Track width + clearance of `net`'s class (the Default class for a net without one;
    `DEFAULT_PITCH_MM` without classes)."""
    classes = {c.name: c for c in model.net_classes}
    name = dict(model.net_class_of).get(net, "Default")
    c = classes.get(name) or classes.get("Default")
    if c is None:
        return DEFAULT_PITCH_MM
    return round(c.track_width + c.clearance, 6)


def signal_pitch(model: BoardModel) -> float:
    """The Default class's pitch (`DEFAULT_PITCH_MM` without one)."""
    c = next((c for c in model.net_classes if c.name == "Default"), None)
    return DEFAULT_PITCH_MM if c is None else round(c.track_width + c.clearance, 6)


def via_pitch(model: BoardModel) -> float:
    """The Default class's via diameter + clearance (room for one via)."""
    c = next((c for c in model.net_classes if c.name == "Default"), None)
    return 0.6 + 0.2 if c is None else round(c.via_diameter + c.clearance, 6)


def net_weight(cls: Classification, net: str) -> float:
    """Signal, diff pair and high-current nets 1.0, supply nets `SUPPLY_WEIGHT`, ground 0."""
    kind = cls.net_kind.get(net, "signal")
    if kind == "ground":
        return 0.0
    return SUPPLY_WEIGHT if kind == "supply" else 1.0


def shared_weight(a: frozenset[str], b: frozenset[str], cls: Classification) -> float:
    """Σ `net_weight` over the nets in both sets."""
    return round(sum(net_weight(cls, n) for n in a & b), 6)


def channel_mm(shared: float, pitch: float, keep: float) -> float:
    """The gap two units sharing `shared` weighted nets need: `CHANNEL_BASE_MM` + one pitch
    per two shared nets (half of them jog along the gap to line up, the rest cross it),
    within [`keep`, `CHANNEL_MAX_MM`] (never below `keep`)."""
    c = CHANNEL_BASE_MM + pitch * math.ceil(max(shared, 0.0) / 2 - _EPS)
    return round(max(min(c, CHANNEL_MAX_MM), keep), 6)


# ---- congestion ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Congestion:
    worst: float  # largest demand / capacity of a cell
    p90: float  # 90th percentile over the cells with any demand
    over: int  # cells with a ratio > 1.0
    cells: int  # cells with any demand
    at: tuple[float, float] | None = None  # centre of the worst cell (board mm)


def _placed(model: BoardModel, cls: Classification) -> list[Box]:
    return [
        box_of(p)
        for p in model.parts
        if cls.part_class.get(p.ref) not in _SKIP
        and len(p.courtyard) + len(p.pads) > 0
        and on_board(model, p)
    ]


def congestion(model: BoardModel, cls: Classification, *, cell: float = CONG_CELL_MM) -> Congestion:
    """RUDY routing demand / capacity per `cell` (module docstring); pads off the board do
    not count. An empty board → all zeros."""
    o = model.outline
    x0, y0 = (o.x, o.y) if o.kind != "none" else (0.0, 0.0)
    demand: dict[tuple[int, int], float] = {}
    pads_area: dict[tuple[int, int], float] = {}
    on = {p.ref for p in model.parts if on_board(model, p)}
    for p in model.parts:
        if p.ref not in on:
            continue
        for q in p.pads:
            k = (math.floor((q.x - x0) / cell), math.floor((q.y - y0) / cell))
            pads_area[k] = pads_area.get(k, 0.0) + q.w * q.h
    nets = sorted({q.net for p in model.parts if p.ref in on for q in p.pads if q.net})
    for net in nets:
        if cls.net_kind.get(net) == "ground":
            continue
        pitch = net_pitch(model, net)
        for a, b in connections(model, net):
            if a.ref not in on or b.ref not in on:
                continue
            length = octi_dist((a.x, a.y), (b.x, b.y))
            if length <= _EPS:
                continue
            bx0, bx1 = sorted((a.x, b.x))
            by0, by1 = sorted((a.y, b.y))
            if bx1 - bx0 < pitch:
                c = (bx0 + bx1) / 2
                bx0, bx1 = c - pitch / 2, c + pitch / 2
            if by1 - by0 < pitch:
                c = (by0 + by1) / 2
                by0, by1 = c - pitch / 2, c + pitch / 2
            dens = length * pitch / ((bx1 - bx0) * (by1 - by0))  # copper mm² per mm²
            for i in range(math.floor((bx0 - x0) / cell), math.floor((bx1 - x0) / cell) + 1):
                cx0 = x0 + i * cell
                ox = min(bx1, cx0 + cell) - max(bx0, cx0)
                if ox <= _EPS:
                    continue
                for j in range(math.floor((by0 - y0) / cell), math.floor((by1 - y0) / cell) + 1):
                    cy0 = y0 + j * cell
                    oy = min(by1, cy0 + cell) - max(by0, cy0)
                    if oy <= _EPS:
                        continue
                    demand[(i, j)] = demand.get((i, j), 0.0) + dens * ox * oy
    if not demand:
        return Congestion(0.0, 0.0, 0, 0)
    ratios: list[float] = []
    worst: tuple[float, tuple[int, int]] | None = None
    for k in sorted(demand):
        room = max(cell * cell - pads_area.get(k, 0.0), 0.1 * cell * cell) * ROUTING_LAYERS
        ratios.append(demand[k] / room)
        if worst is None or ratios[-1] > worst[0] + _EPS:
            worst = (ratios[-1], k)
    assert worst is not None
    srt = sorted(ratios)
    p90 = srt[min(len(srt) - 1, math.ceil(0.9 * len(srt)) - 1)]
    return Congestion(
        worst=round(srt[-1], 3),
        p90=round(p90, 3),
        over=sum(1 for r in ratios if r > 1.0 + _EPS),
        cells=len(ratios),
        at=(round(x0 + (worst[1][0] + 0.5) * cell, 3), round(y0 + (worst[1][1] + 0.5) * cell, 3)),
    )


# ---- largest hole -------------------------------------------------------------------------------


def largest_hole(model: BoardModel, cls: Classification, *, grow: float = 0.5) -> float:
    """mm² of the largest axis-aligned rectangle inside the courtyards' union bounding box
    (clipped to the usable box) that no courtyard box (grown by `grow`) touches; 0.0 without
    an outline or parts. Mounting holes and fiducials do not count as parts."""
    o = model.outline
    boxes = _placed(model, cls)
    if o.kind == "none" or not boxes:
        return 0.0
    m = edge_margin(model)
    ux0, uy0 = max(min(b[0] for b in boxes), o.x + m), max(min(b[1] for b in boxes), o.y + m)
    ux1 = min(max(b[2] for b in boxes), o.x + o.w - m)
    uy1 = min(max(b[3] for b in boxes), o.y + o.h - m)
    g = HOLE_GRID_MM
    nx, ny = int((ux1 - ux0) / g + _EPS), int((uy1 - uy0) / g + _EPS)
    if nx <= 0 or ny <= 0:
        return 0.0
    blocked = [bytearray(nx) for _ in range(ny)]
    for b in boxes:
        i0 = max(math.floor((b[0] - grow - ux0) / g), 0)
        i1 = min(math.ceil((b[2] + grow - ux0) / g), nx)
        j0 = max(math.floor((b[1] - grow - uy0) / g), 0)
        j1 = min(math.ceil((b[3] + grow - uy0) / g), ny)
        for j in range(j0, j1):
            row = blocked[j]
            for i in range(i0, i1):
                row[i] = 1
    heights = [0] * nx
    best = 0
    for j in range(ny):
        row = blocked[j]
        for i in range(nx):
            heights[i] = 0 if row[i] else heights[i] + 1
        stack: list[int] = []  # the largest rectangle in a histogram
        for i in range(nx + 1):
            h = heights[i] if i < nx else 0
            while stack and heights[stack[-1]] >= h:
                top = heights[stack.pop()]
                left = stack[-1] + 1 if stack else 0
                best = max(best, top * (i - left))
            stack.append(i)
    return round(best * g * g, 3)


# ---- block gaps ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class GapRow:
    a: str
    b: str
    shared: float  # weighted shared nets
    gap: float  # least courtyard-box gap between their parts on the board (mm)
    required: float  # channel_mm(shared)

    @property
    def excess(self) -> float:
        return round(self.gap - self.required, 3)


def _box_gap(a: Box, b: Box) -> float:
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return math.hypot(dx, dy) if dx > 0 and dy > 0 else max(dx, dy)


def block_gaps(
    model: BoardModel, cls: Classification, blocks: Sequence[BlockProposal], *, keep: float
) -> tuple[GapRow, ...]:
    """Every pair of blocks sharing a weighted net: the gap between their placed parts and
    the channel they need; pairs in name order."""
    on = {p.ref: p for p in model.parts if on_board(model, p)}
    pitch = signal_pitch(model)
    members: dict[str, list[str]] = {
        b.name: sorted((r for r in b.refs if r in on), key=natural_key) for b in blocks
    }
    nets = {
        n: frozenset(q.net for r in refs for q in on[r].pads if q.net)
        for n, refs in members.items()
    }
    boxes = {n: [box_of(on[r]) for r in refs] for n, refs in members.items()}
    names = sorted((n for n in members if members[n]), key=natural_key)
    rows: list[GapRow] = []
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            w = shared_weight(nets[a], nets[b], cls)
            if w <= 0:
                continue
            gap = min(_box_gap(x, y) for x in boxes[a] for y in boxes[b])
            rows.append(GapRow(a, b, w, round(gap, 3), channel_mm(w, pitch, keep)))
    return tuple(rows)


def gap_summary(rows: Sequence[GapRow]) -> Mapping[str, float]:
    """`worst`: the largest gap - required of the pairs sharing a signal net (weight ≥ 1;
    pairs tied only by supply nets may sit anywhere); `mean`: the shared-weight mean of
    the excess over every pair. 0.0 without rows."""
    if not rows:
        return {"worst": 0.0, "mean": 0.0}
    total = sum(r.shared for r in rows)
    mean = sum(r.excess * r.shared for r in rows) / total if total > 0 else 0.0
    signal = [r.excess for r in rows if r.shared >= 1.0 - _EPS]
    return {"worst": max(signal, default=0.0), "mean": round(mean, 3)}
