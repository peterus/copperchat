"""The staging area for footprints that are not placed yet (B-C11, spec §7.2 staging area).

`pcb_sync_from_schematic` puts every new footprint here: a column-major grid right of the
board outline, so nothing lands on the board itself and no two courtyards overlap. P4's
placer re-exports `Move`. Pure: no kipy, deterministic.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from copperchat.board.model import BoardModel, Pad, Part, Point, Side

STAGING_GAP_MM = 5.0  # distance of the staging area from the outline / the other parts
CELL_GAP_MM = 1.0  # added to every part's courtyard box
COLUMN_HEIGHT_MM = 100.0  # column height without an outline
# KiCad stores coordinates as int32 nm (±2147 mm): the columns wrap into a new band below
# before they pass this x.
MAX_STAGING_X_MM = 2000.0


@dataclass(frozen=True)
class Move:
    ref: str
    x: float
    y: float
    rot: float
    side: Side


Box = tuple[float, float, float, float]  # x0, y0, x1, y1


def natural_key(ref: str) -> tuple[tuple[int, int | str], ...]:
    """ "R2" < "R10": digit runs compare as numbers."""
    return tuple(
        (0, int(tok)) if tok.isdigit() else (1, tok) for tok in re.findall(r"\d+|\D+", ref)
    )


def _pad_corners(q: Pad) -> list[Point]:
    """The bounding-box corners of a (rotated) pad: `w`/`h` are the unrotated sizes."""
    t = math.radians(q.rot)
    c, s = abs(math.cos(t)), abs(math.sin(t))
    hx, hy = q.w / 2 * c + q.h / 2 * s, q.w / 2 * s + q.h / 2 * c
    return [(q.x - hx, q.y - hy), (q.x + hx, q.y + hy)]


def part_box(p: Part) -> Box:
    """The courtyard's bounding box; without a courtyard the pads', else the origin."""
    pts = list(p.courtyard) or [c for q in p.pads for c in _pad_corners(q)]
    if not pts:
        return (p.x, p.y, p.x, p.y)
    xs, ys = [q[0] for q in pts], [q[1] for q in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _origin(model: BoardModel, staged: set[str]) -> tuple[float, float, float]:
    """(x, y, column height) of the staging area's top-left corner: right of the outline
    and of every part already waiting right of it (staged by an earlier sync), else
    right of the other parts, else (0, 0)."""
    o = model.outline
    others = [part_box(p) for p in model.parts if p.ref not in staged]
    if o.kind != "none":
        right = o.x + o.w
        waiting = [b[2] for b in others if b[0] >= right]
        return (max([right, *waiting]) + STAGING_GAP_MM, o.y, o.h)
    if others:
        return (
            max(b[2] for b in others) + STAGING_GAP_MM,
            min(b[1] for b in others),
            COLUMN_HEIGHT_MM,
        )
    return (0.0, 0.0, COLUMN_HEIGHT_MM)


def staging_positions(model: BoardModel, refs: Sequence[str]) -> tuple[Move, ...]:
    """Moves that put `refs` (unknown ones ignored) into the staging area: a column-major
    grid in natural ref order, right of the outline (or of the other parts, or at
    (0, 0) on an empty board); cell = courtyard box + 1 mm; a column ends at the
    outline height (100 mm without an outline); rotation and side unchanged."""
    wanted = set(refs)
    parts = sorted(
        (p for p in model.parts if p.ref in wanted), key=lambda p: (natural_key(p.ref), p.kiid)
    )
    x0, top, height = _origin(model, wanted)
    moves: list[Move] = []
    col_x, y, col_w, band_h = x0, top, 0.0, 0.0
    for p in parts:
        bx0, by0, bx1, by1 = part_box(p)
        w, h = bx1 - bx0 + CELL_GAP_MM, by1 - by0 + CELL_GAP_MM
        if y > top and y + h - CELL_GAP_MM > top + height:
            col_x, y, col_w = col_x + col_w, top, 0.0
            if col_x > x0 and col_x + w > MAX_STAGING_X_MM:  # next band, below this one
                top += max(height, band_h) + STAGING_GAP_MM
                col_x, y, band_h = x0, top, 0.0
        moves.append(Move(p.ref, col_x + (p.x - bx0), y + (p.y - by0), p.rot, p.side))
        y += h
        col_w = max(col_w, w)
        band_h = max(band_h, y - top)
    return tuple(moves)
