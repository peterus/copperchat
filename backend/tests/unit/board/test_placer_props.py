"""P4 placer: seeded property tests of `place_block` (B-C11) over random boards.

Each seed (0..249) builds one random board: an outline 20-80 mm, 0-3 footprint keepouts,
0-3 locked parts already placed legally, 2-30 block parts (courtyards 0.5-8 mm, random pads
and nets) waiting in the staging area right of the outline. `place_block` puts the block
into the whole outline; every case is computed once and shared by all properties."""

from __future__ import annotations

import functools
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

from copperchat.board.classify import classify
from copperchat.board.geom import rect_poly
from copperchat.board.model import BoardModel, Pad, Part, Point, Rect, Side
from copperchat.board.placer import (
    DoesNotFit,
    Move,
    PlaceResult,
    apply_moves,
    block_layout,
    edge_margin,
    moved_part,
    place_block,
)
from copperchat.board.placer.layout import box_of

from .builders import board, outline, part, zone
from .builders_circuit import pad

SEEDS = range(250)
GRID = 0.05
TOP: frozenset[Side] = frozenset({"top"})
_PREFIX = ("R", "C", "U", "J", "D", "L", "Q")

Box = tuple[float, float, float, float]


def gap(a: Box, b: Box) -> float:
    """Separation of two boxes (negative: they overlap)."""
    return max(b[0] - a[2], a[0] - b[2], b[1] - a[3], a[1] - b[3])


def overlap_area(a: Box, b: Box) -> float:
    return max(min(a[2], b[2]) - max(a[0], b[0]), 0.0) * max(min(a[3], b[3]) - max(a[1], b[1]), 0.0)


def random_part(
    rnd: random.Random, ref: str, at: Point, nets: Sequence[str], *, locked: bool = False
) -> Part:
    """A part at `at` with a w x h courtyard (0.5-8 mm) and 1-8 pads inside it."""
    w, h = round(rnd.uniform(0.5, 8.0), 2), round(rnd.uniform(0.5, 8.0), 2)
    x, y = at
    n = rnd.choice((1, 2, 2, 2, 3, 4, 6, 8))
    pads: list[Pad] = []
    for k in range(n):
        pw, ph = min(0.4, w / 2), min(0.4, h / 2)
        px = x + rnd.uniform(-w / 2 + pw / 2, w / 2 - pw / 2)
        py = y + rnd.uniform(-h / 2 + ph / 2, h / 2 - ph / 2)
        pads.append(pad(ref, str(k + 1), rnd.choice(nets), (px, py), w=pw, h=ph))
    return part(
        ref,
        x,
        y,
        locked=locked,
        rot=rnd.choice((0.0, 90.0)) if not locked else 0.0,
        pads=tuple(pads),
        courtyard=tuple(rect_poly(x - w / 2, y - h / 2, w, h)),
    )


@dataclass(frozen=True)
class Case:
    seed: int
    model: BoardModel
    block: tuple[str, ...]  # the block refs (incl. locked ones)
    locked: tuple[str, ...]
    keepouts: tuple[Box, ...]
    inner: Box  # outline minus the edge margin
    result: PlaceResult | DoesNotFit


def _legal_spot(
    rnd: random.Random, p: Part, inner: Box, taken: Sequence[Box], tries: int = 50
) -> Part | None:
    """`p` moved to a random spot inside `inner` clear of `taken` (≥ 0.5 mm), or None."""
    b = box_of(p)
    w, h = b[2] - b[0], b[3] - b[1]
    for _ in range(tries):
        if inner[2] - inner[0] - w <= 0 or inner[3] - inner[1] - h <= 0:
            return None
        x0 = rnd.uniform(inner[0], inner[2] - w)
        y0 = rnd.uniform(inner[1], inner[3] - h)
        cand = (x0, y0, x0 + w, y0 + h)
        if all(gap(cand, t) >= 0.5 for t in taken):
            dx, dy = x0 - b[0], y0 - b[1]
            return apply_moves(
                board(parts=(p,)), [Move(p.ref, p.x + dx, p.y + dy, p.rot, p.side)]
            ).parts[0]
    return None


def make_board(seed: int) -> tuple[BoardModel, tuple[str, ...], tuple[str, ...], tuple[Box, ...]]:
    """(model, block refs, locked refs, keepout boxes) of `seed`."""
    rnd = random.Random(seed)
    ow, oh = round(rnd.uniform(20, 80), 1), round(rnd.uniform(20, 80), 1)
    ox, oy = round(rnd.uniform(-10, 10), 1), round(rnd.uniform(-10, 10), 1)
    rules: list[tuple[str, float]] = [("min_clearance", 0.127)]
    if rnd.random() < 0.3:
        rules.append(("copper_edge_clearance", rnd.choice((0.2, 0.5, 1.0))))
    probe = board(parts=(), outline=outline(ox, oy, ow, oh), rules=tuple(rules))
    margin = edge_margin(probe)
    inner = (ox + margin, oy + margin, ox + ow - margin, oy + oh - margin)
    nets = [f"N{i}" for i in range(rnd.randint(2, 12))] + ["GND", "+3V3"]

    keep: list[Box] = []
    for _ in range(rnd.randint(0, 3)):
        kw, kh = rnd.uniform(2, ow / 4), rnd.uniform(2, oh / 4)
        kx, ky = rnd.uniform(ox, ox + ow - kw), rnd.uniform(oy, oy + oh - kh)
        keep.append((kx, ky, kx + kw, ky + kh))
    zones = tuple(
        zone(
            f"K{i}",
            net="",
            poly=tuple(rect_poly(k[0], k[1], k[2] - k[0], k[3] - k[1])),
            rule_area=True,
            keepout=frozenset({"footprints"}),
            kiid=f"kz{i}",
        )
        for i, k in enumerate(keep)
    )

    parts: list[Part] = []
    locked: list[str] = []
    taken: list[Box] = list(keep)
    for i in range(rnd.randint(0, 3)):
        p = random_part(rnd, f"X{i + 1}", (0.0, 0.0), nets, locked=True)
        q = _legal_spot(rnd, p, inner, taken)
        if q is not None:
            parts.append(q)
            locked.append(q.ref)
            taken.append(box_of(q))

    n = rnd.randint(2, 30)
    sx = ox + ow + 10.0  # staging column right of the outline
    sy = oy
    block: list[str] = []
    for i in range(n):
        ref = f"{rnd.choice(_PREFIX)}{i + 1}"
        p = random_part(rnd, ref, (sx, sy + 4.0), nets)
        parts.append(p)
        block.append(ref)
        sy += 10.0
    # some locked parts are named in the block too: they must stay put
    block.extend(r for r in locked if rnd.random() < 0.5)
    m = board(parts=tuple(parts), outline=outline(ox, oy, ow, oh), rules=tuple(rules), zones=zones)
    return m, tuple(block), tuple(locked), tuple(keep)


@functools.cache
def case(seed: int) -> Case:
    m, block, locked, keep = make_board(seed)
    margin = edge_margin(m)
    o = m.outline
    inner = (o.x + margin, o.y + margin, o.x + o.w - margin, o.y + o.h - margin)
    result = place_block(
        m,
        classify(m),
        block,
        Rect(o.x, o.y, o.w, o.h),
        side="top",
        rotation=None,
        allowed_sides=TOP,
    )
    return Case(seed, m, block, locked, keep, inner, result)


def placed_cases() -> list[tuple[Case, BoardModel]]:
    out = []
    for s in SEEDS:
        c = case(s)
        if isinstance(c.result, PlaceResult):
            out.append((c, apply_moves(c.model, c.result.moves)))
    return out


def on_board(c: Case, p: Part) -> bool:
    o = c.model.outline
    b = box_of(p)
    return b[0] < o.x + o.w and o.x < b[2] and b[1] < o.y + o.h and o.y < b[3]


def sparse(c: Case) -> bool:
    """Σ of all courtyards (block, locked) and keepouts ≤ 25 % of the outline."""
    o = c.model.outline
    area = sum((b[2] - b[0]) * (b[3] - b[1]) for b in (box_of(p) for p in c.model.parts))
    area += sum((k[2] - k[0]) * (k[3] - k[1]) for k in c.keepouts)
    return area <= 0.25 * o.w * o.h


KEEP_CHANNELS = 0.5


def block_box_fits(c: Case) -> bool:
    """The rigid block layout's bounding box (either orientation), grown by one placement
    grid step of slack, has a spot inside the outline minus the margin, clear of keepouts
    and `KEEP_CHANNELS` away from the locked parts. Then some 0.5 mm grid position of the
    block is legal (every member courtyard lies inside that box), so `place_block` must
    not answer `DoesNotFit`."""
    m = c.model
    members = [r for r in c.block if r not in c.locked]
    mv = block_layout(m, classify(m), members, keep_channels=KEEP_CHANNELS)
    bs = [box_of(moved_part(m.part(x.ref), x)) for x in mv]
    w = max(b[2] for b in bs) - min(b[0] for b in bs) + 0.5
    h = max(b[3] for b in bs) - min(b[1] for b in bs) + 0.5
    locked = [box_of(m.part(r)) for r in c.locked]
    i = c.inner
    for bw, bh in ((w, h), (h, w)):
        nx = math.floor((i[2] - i[0] - bw) / 0.5 + 1e-9)
        ny = math.floor((i[3] - i[1] - bh) / 0.5 + 1e-9)
        for ky in range(ny + 1):
            for kx in range(nx + 1):
                x0, y0 = i[0] + 0.5 * kx, i[1] + 0.5 * ky
                b = (x0, y0, x0 + bw, y0 + bh)
                if any(overlap_area(b, k) > 0 for k in c.keepouts):
                    continue
                if any(gap(b, lb) < KEEP_CHANNELS + 1e-6 for lb in locked):
                    continue
                return True
    return False


# ---- properties ----------------------------------------------------------------------------


def test_no_overlaps() -> None:
    bad = []
    for c, after in placed_cases():
        parts = [p for p in after.parts if on_board(c, p)]
        for i, a in enumerate(parts):
            for b in parts[i + 1 :]:
                if a.side == b.side and gap(box_of(a), box_of(b)) < -1e-6:
                    bad.append((c.seed, a.ref, b.ref))
    assert not bad, f"overlaps (seed, ref, ref): {bad[:10]}"


def test_inside_outline_minus_margin_and_keepouts() -> None:
    bad = []
    for c, after in placed_cases():
        moved = {mv.ref for mv in c.result.moves}  # type: ignore[union-attr]
        assert set(c.block) - set(c.locked) <= moved, f"seed {c.seed}: unplaced block parts"
        for ref in moved:
            b = box_of(after.part(ref))
            i = c.inner
            if b[0] < i[0] - 1e-6 or b[1] < i[1] - 1e-6 or b[2] > i[2] + 1e-6 or b[3] > i[3] + 1e-6:
                bad.append((c.seed, ref, "outline"))
            for k in c.keepouts:
                if overlap_area(b, k) > 1e-6:
                    bad.append((c.seed, ref, "keepout"))
    assert not bad, f"violations (seed, ref, what): {bad[:10]}"


def test_locks_never_move() -> None:
    bad: list[tuple[int, str]] = []
    for s in SEEDS:
        c = case(s)
        if not isinstance(c.result, PlaceResult):
            continue
        moved = {mv.ref for mv in c.result.moves}
        bad.extend((s, r) for r in c.locked if r in moved)
    assert not bad, f"locked parts moved (seed, ref): {bad}"


def test_grid_snap_005() -> None:
    bad = []
    for s in SEEDS:
        c = case(s)
        if not isinstance(c.result, PlaceResult):
            continue
        for mv in c.result.moves:
            for v in (mv.x, mv.y):
                if abs(v / GRID - round(v / GRID)) * GRID > 1e-6:
                    bad.append((s, mv))
    assert not bad, f"off-grid moves (seed, move): {bad[:10]}"


def test_never_does_not_fit_when_sparse() -> None:
    """Σ courtyards ≤ 25 % of the outline → never `DoesNotFit` — for blocks whose rigid
    layout has room at all: `place_block` places the block as one rigid layout (B-C11
    candidates = block positions), so a sparse board whose free space is cut up by
    keepouts / locked parts, or a block layout longer than the board is narrow, may
    legitimately not fit; those seeds are counted but not asserted."""
    n_sparse = n_room = 0
    bad: list[tuple[int, DoesNotFit]] = []
    for s in SEEDS:
        c = case(s)
        if not sparse(c):
            continue
        n_sparse += 1
        if not block_box_fits(c):
            continue
        n_room += 1
        if isinstance(c.result, DoesNotFit):
            bad.append((s, c.result))
    print(f"sparse seeds: {n_sparse}, with room for the rigid block: {n_room}")
    assert n_room >= 25, (n_sparse, n_room)  # the property is exercised
    assert not bad, f"DoesNotFit on sparse boards (seed, result): {bad[:5]}"


def test_some_cases_place() -> None:
    """Sanity: the generator is not so dense that nothing is ever placed."""
    placed = sum(isinstance(case(s).result, PlaceResult) for s in SEEDS)
    assert placed >= len(SEEDS) // 2, placed


def test_cases_report_needed_area() -> None:
    for s in SEEDS:
        r = case(s).result
        if isinstance(r, DoesNotFit):
            assert r.needed_mm2 > 0 and r.refs, f"seed {s}: {r}"
            assert math.isfinite(r.available_mm2), f"seed {s}: {r}"
