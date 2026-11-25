"""Block packing (B-C11, spec §9.2): one block = its clusters laid out around their anchors
and shelf-packed, then placed as a whole at the cheapest legal spot of a region. Pure,
deterministic (no randomness; ties by (y, x, rotation)).

Candidates: the block's reference point (its first anchor's origin) on every 0.5 mm grid
position x the four block rotations (only `rotation` when given), the block box inside the
region and the outline minus the edge margin. Legal: no member courtyard closer than
`keep_channels` to a part already on the board, a locked member or a `fixed` part, none in
a footprint keepout. Cost: airwire of the block's non-ground pads to the nearest placed pad
of their net + `PULL_WEIGHT` x n x octilinear distance of the block centre to `target`
(else the region centre) + `EDGE_WEIGHT` x each connector's distance to its edge
(`board.md` Placement edges, else `edge`; without either only a connector that anchors its
own cluster is pulled, to the nearest outline edge), its mouth (the courtyard side away
from its pads) facing that edge. The search runs on a 2 mm grid first (plus the region's
ends) and refines the best spots on the 0.5 mm grid (a full 0.5 mm scan of a rotation the
coarse grid finds nothing legal for)."""

from __future__ import annotations

import dataclasses
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from copperchat.board.boardmd import BoardDoc, parse_board_md
from copperchat.board.classify import Classification, classify
from copperchat.board.geom import octi_dist
from copperchat.board.model import BoardModel, Part, Point, Rect, Side
from copperchat.board.placer.cluster import build_clusters
from copperchat.board.placer.layout import (
    Box,
    Move,
    apply_moves,
    box_of,
    boxes_hit,
    layout_cluster,
    moved_part,
    rotate,
    snap_up,
)
from copperchat.board.placer.legalize import (
    DoesNotFit,
    Outline,
    _hits_keepout,
    courtyard_clearance,
    edge_margin,
    keepout_polys,
    legalize,
    on_board,
)
from copperchat.board.placer.score import PlacementScore, score_placement
from copperchat.board.staging import natural_key

Edge = Literal["top", "bottom", "left", "right"]

PLACE_GRID_MM = 0.5
COARSE_GRID_MM = 2.0
REFINE_KEEP = 6  # best coarse spots per rotation refined on the fine grid
PULL_WEIGHT = 0.1  # per member and mm; connectivity (airwire) dominates inside the region
EDGE_WEIGHT = 3.0
EDGE_SNAP_MM = 0.5  # an assigned connector this close to its edge (beyond the margin) is on it
EDGE_MISS = 1000.0
FACING_MM = 5.0  # a connector whose mouth does not face its edge costs like this far away
MOUTH_MIN_MM = 0.5  # pad centroid this far off the courtyard centre gives the connector a mouth
_EDGE_NORMAL: Mapping[str, Point] = {
    "left": (-1.0, 0.0),
    "right": (1.0, 0.0),
    "top": (0.0, -1.0),
    "bottom": (0.0, 1.0),
}
_EPS = 1e-9


@dataclass(frozen=True)
class PlaceResult:
    moves: tuple[Move, ...]
    score: PlacementScore


def _area(b: Box) -> float:
    return max(b[2] - b[0], 0.0) * max(b[3] - b[1], 0.0)


def _union_box(boxes: Sequence[Box]) -> Box:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def block_layout(
    model: BoardModel, cls: Classification, refs: Sequence[str], *, keep_channels: float
) -> tuple[Move, ...]:
    """The block's members relative to its first anchor at (0, 0), rotation 0, top: every
    cluster laid out, clusters shelf-packed (largest box first) `keep_channels` apart."""
    gap = courtyard_clearance(model) + 0.05
    layouts: list[tuple[tuple[Move, ...], Box, str]] = []
    for c in build_clusters(model, cls, refs):
        mv = layout_cluster(model, cls, c, gap=gap)
        b = _union_box([box_of(moved_part(model.part(m.ref), m)) for m in mv])
        layouts.append((mv, b, mv[0].ref))
    if not layouts:
        return ()
    layouts.sort(key=lambda t: (-_area(t[1]), natural_key(t[2])))
    kc = keep_channels
    total = sum((b[2] - b[0] + kc) * (b[3] - b[1] + kc) for _, b, _ in layouts)
    width = max(max(b[2] - b[0] for _, b, _ in layouts), math.sqrt(total))
    out: list[Move] = []
    x = y = row_h = 0.0
    first = layouts[0][1]
    for mv, b, _ in layouts:
        w, h = b[2] - b[0], b[3] - b[1]
        if out and x > 0 and x + w > width + _EPS:
            x, y, row_h = 0.0, y + row_h + kc, 0.0
        # translate so the cluster box starts at (x, y) of the first cluster's box frame
        dx = snap_up(first[0] + x - b[0]) if out else 0.0
        dy = snap_up(first[1] + y - b[1]) if out else 0.0
        out.extend(Move(m.ref, m.x + dx, m.y + dy, m.rot, m.side) for m in mv)
        x += w + kc
        row_h = max(row_h, h)
    return tuple(out)


def block_extent(
    model: BoardModel, cls: Classification, refs: Sequence[str], *, keep_channels: float
) -> float:
    """The block's largest layout extent (mm): the edge-strip depth of `pcb_place_block`."""
    mv = block_layout(model, cls, refs, keep_channels=keep_channels)
    if not mv:
        return 0.0
    b = _union_box([box_of(moved_part(model.part(m.ref), m)) for m in mv])
    return max(b[2] - b[0], b[3] - b[1])


def _orient(mv: Sequence[Move], theta: int, side: Side) -> list[Move]:
    """The local layout rotated by `theta` about the reference point, flipped left/right
    for the bottom side first."""
    out = []
    for m in mv:
        x, y, rot = m.x, m.y, m.rot
        if side == "bottom":
            x, rot = -x, (180.0 - rot) % 360
        rx, ry = rotate((x, y), theta)
        out.append(Move(m.ref, round(rx, 6), round(ry, 6), (rot + theta) % 360, side))
    return out


@dataclass
class _Variant:
    theta: int
    moves: list[Move]
    boxes: list[Box]
    box: Box
    pads: list[tuple[str, Point]]  # (net, local position) of non-ground pads with outside partners
    conns: Sequence[tuple[Box, Point | None, str | None]]  # connectors: local box, mouth, edge


def _mouth(p: Part) -> Point | None:
    """Direction of the connector's mouth: from its pad centroid through its courtyard
    centre; None when the pads sit in the middle (pin headers)."""
    if not p.pads:
        return None
    b = box_of(p)
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    px = sum(q.x for q in p.pads) / len(p.pads)
    py = sum(q.y for q in p.pads) / len(p.pads)
    dx, dy = cx - px, cy - py
    n = math.hypot(dx, dy)
    if n < MOUTH_MIN_MM:
        return None
    return (dx / n, dy / n)


def _edge_cost(b: Box, mouth: Point | None, edge: str | None, ob: Box, margin: float) -> float:
    """Distance of a connector box `b` to `edge` of the outline box `ob` (nearest edge when
    None), plus `FACING_MM` x how far its mouth turns away from that edge. An assigned edge
    is nearly hard: farther than `EDGE_SNAP_MM` beyond the edge margin costs `EDGE_MISS`
    (only spots where no candidate reaches the edge fall back to the distance)."""
    d = {"left": b[0] - ob[0], "right": ob[2] - b[2], "top": b[1] - ob[1], "bottom": ob[3] - b[3]}
    e = edge if edge in d else min(d, key=lambda k: (d[k], list(d).index(k)))
    cost = max(d[e], 0.0)
    if edge in d and d[e] > margin + EDGE_SNAP_MM + _EPS:
        cost += EDGE_MISS
    if mouth is not None:
        n = _EDGE_NORMAL[e]
        cost += FACING_MM * (1.0 - (mouth[0] * n[0] + mouth[1] * n[1])) / 2.0
    return cost


def place_block(
    model: BoardModel,
    cls: Classification,
    block_refs: Sequence[str],
    region: Rect,
    *,
    side: Side,
    rotation: int | None,
    keep_channels: float = 0.5,
    fixed: Sequence[Move] = (),
    allowed_sides: frozenset[Side],
    edge: Edge | None = None,
    target: Point | None = None,
    doc: BoardDoc | None = None,
) -> PlaceResult | DoesNotFit:
    """Place the block's unlocked members (B-C11). `fixed` parts are put exactly where
    given first and returned with the moves; locked members stay where they are. Raises
    `ValueError` when an SMD member would land on a side not in `allowed_sides`."""
    doc = (
        doc
        if doc is not None
        else dataclasses.replace(parse_board_md(""), allowed_sides=allowed_sides)
    )
    known = {p.ref: p for p in model.parts}
    fixed_refs = {m.ref for m in fixed}
    members = sorted(
        {r for r in block_refs if r in known and not known[r].locked and r not in fixed_refs},
        key=natural_key,
    )
    if side not in allowed_sides and any(q.smd for r in members for q in known[r].pads):
        raise ValueError(f"SMD parts on the {side} side are not allowed")
    for m in fixed:
        if m.side not in allowed_sides and m.ref in known and any(q.smd for q in known[m.ref].pads):
            raise ValueError(f"SMD parts on the {m.side} side are not allowed")
    base = apply_moves(model, fixed)
    kc = keep_channels
    margin = edge_margin(base)
    outline = Outline(base, margin)
    keepouts = keepout_polys(base)
    mset = set(members)
    obstacles = [
        box_of(p)
        for p in base.parts
        if p.ref not in mset
        and p.side == side
        and len(p.courtyard) + len(p.pads) > 0
        and (on_board(base, p) or p.ref in fixed_refs or p.locked)
    ]
    reg: Box = (region.x, region.y, region.x + region.w, region.y + region.h)
    allowed = (
        max(reg[0], outline.inner[0]),
        max(reg[1], outline.inner[1]),
        min(reg[2], outline.inner[2]),
        min(reg[3], outline.inner[3]),
    )

    def does_not_fit() -> DoesNotFit:
        needed = sum(
            (b[2] - b[0] + kc) * (b[3] - b[1] + kc) for b in (box_of(known[r]) for r in members)
        )
        taken = sum(
            _area(
                (
                    max(b[0], allowed[0]),
                    max(b[1], allowed[1]),
                    min(b[2], allowed[2]),
                    min(b[3], allowed[3]),
                )
            )
            for b in obstacles
        )
        return DoesNotFit(
            needed_mm2=round(needed, 3),
            available_mm2=round(max(_area(allowed) - taken, 0.0), 3),
            refs=tuple(members),
        )

    if not members:
        return PlaceResult(tuple(fixed), score_placement(base, cls, doc))
    local = block_layout(base, cls, members, keep_channels=kc)
    # connectors pulled to an edge: assigned ones and those anchoring a cluster (a header
    # that serves an MCU follows the MCU instead of dragging its block to an edge)
    heads = {c.members[0] for c in build_clusters(base, cls, members)}

    # outside pads per net (parts on the board that are not members)
    outside: dict[str, list[Point]] = defaultdict(list)
    for p in base.parts:
        if p.ref in mset or not (on_board(base, p) or p.ref in fixed_refs):
            continue
        for q in p.pads:
            if q.net and cls.net_kind.get(q.net) != "ground":
                outside[q.net].append((q.x, q.y))
    edges = {row.ref: row.edge for row in doc.edges if row.edge is not None}
    o = base.outline
    obox: Box = (o.x, o.y, o.x + o.w, o.y + o.h) if o.kind != "none" else reg

    variants: list[_Variant] = []
    for theta in (rotation,) if rotation is not None else (0, 90, 180, 270):
        mv = _orient(local, theta % 360, side)
        parts = [moved_part(known[m.ref], m) for m in mv]
        boxes = [box_of(p) for p in parts]
        pads = [(q.net, (q.x, q.y)) for p in parts for q in p.pads if q.net and q.net in outside]
        conns = [
            (box_of(p), _mouth(p), edges.get(p.ref, edge))
            for p in parts
            if cls.part_class.get(p.ref) == "connector"
            and (p.ref in edges or edge is not None or p.ref in heads)
        ]
        variants.append(_Variant(theta % 360, mv, boxes, _union_box(boxes), pads, conns))

    centre = target if target is not None else ((reg[0] + reg[2]) / 2, (reg[1] + reg[3]) / 2)
    n = len(members)

    def legal(v: _Variant, x: float, y: float) -> bool:
        bb = (v.box[0] + x, v.box[1] + y, v.box[2] + x, v.box[3] + y)
        if not outline.contains(bb) and not all(
            outline.contains((b[0] + x, b[1] + y, b[2] + x, b[3] + y)) for b in v.boxes
        ):
            return False
        near = [ob for ob in obstacles if boxes_hit(bb, ob, kc)]
        for b in v.boxes:
            mb = (b[0] + x, b[1] + y, b[2] + x, b[3] + y)
            if any(boxes_hit(mb, ob, kc) for ob in near):
                return False
            if keepouts and _hits_keepout(mb, keepouts) is not None:
                return False
        return True

    def cost(v: _Variant, x: float, y: float) -> float:
        c = 0.0
        for net, (px, py) in v.pads:
            c += min(octi_dist((px + x, py + y), q) for q in outside[net])
        bc = ((v.box[0] + v.box[2]) / 2 + x, (v.box[1] + v.box[3]) / 2 + y)
        c += PULL_WEIGHT * n * octi_dist(bc, centre)
        for b, mouth, e in v.conns:
            c += EDGE_WEIGHT * _edge_cost(
                (b[0] + x, b[1] + y, b[2] + x, b[3] + y), mouth, e, obox, margin
            )
        return round(c, 6)

    def axis(lo: float, hi: float, step: float) -> list[float]:
        """Multiples of `step` in [lo, hi], plus both ends on the 0.5 mm grid (a tight
        fit between two obstacles is never skipped by the coarse grid)."""
        if lo > hi + _EPS:
            return []
        out = {
            i * step for i in range(math.ceil(lo / step - 1e-9), math.floor(hi / step + 1e-9) + 1)
        }
        g = PLACE_GRID_MM
        first, last = math.ceil(lo / g - 1e-9) * g, math.floor(hi / g + 1e-9) * g
        out.update(v for v in (first, last) if lo - _EPS <= v <= hi + _EPS)
        return sorted(round(v, 6) for v in out)

    def positions(v: _Variant, step: float, window: Box | None = None) -> list[tuple[float, float]]:
        lo_x, hi_x = allowed[0] - v.box[0], allowed[2] - v.box[2]
        lo_y, hi_y = allowed[1] - v.box[1], allowed[3] - v.box[3]
        if window is not None:
            lo_x, lo_y = max(lo_x, window[0]), max(lo_y, window[1])
            hi_x, hi_y = min(hi_x, window[2]), min(hi_y, window[3])
        return [(x, y) for y in axis(lo_y, hi_y, step) for x in axis(lo_x, hi_x, step)]

    Found = tuple[float, float, float, int]  # cost, y, x, variant index

    def scan(vi: int, step: float, windows: Sequence[Box] | None) -> list[Found]:
        v = variants[vi]
        spots: list[tuple[float, float]] = []
        if windows is None:
            spots = positions(v, step)
        else:
            seen: set[tuple[float, float]] = set()
            for w in windows:
                for spot in positions(v, step, w):
                    if spot not in seen:
                        seen.add(spot)
                        spots.append(spot)
        return [(cost(v, x, y), y, x, vi) for x, y in spots if legal(v, x, y)]

    fine: list[Found] = []
    for vi in range(len(variants)):
        coarse = sorted(scan(vi, COARSE_GRID_MM, None))
        if not coarse:  # nothing on the coarse grid: scan this rotation fully
            fine.extend(scan(vi, PLACE_GRID_MM, None))
            continue
        d = COARSE_GRID_MM
        wins = [(x - d, y - d, x + d, y + d) for _, y, x, _ in coarse[:REFINE_KEEP]]
        fine.extend(scan(vi, PLACE_GRID_MM, wins))
    if not fine:
        return does_not_fit()
    _, y, x, vi = min(fine)
    v = variants[vi]
    moves = [Move(m.ref, round(m.x + x, 6), round(m.y + y, 6), m.rot, m.side) for m in v.moves]
    legal_moves = legalize(
        base,
        moves,
        movable=frozenset(members),
        max_shift=None,
        keepouts=keepouts,
        edge_margin=margin,
    )
    if isinstance(legal_moves, DoesNotFit):
        return legal_moves
    final = apply_moves(base, legal_moves)
    # the served pins of decoupling caps follow the new positions: classify again
    return PlaceResult(
        tuple(fixed) + tuple(legal_moves), score_placement(final, classify(final, doc), doc)
    )
