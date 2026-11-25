"""P4 placer: seeded property tests of `legalize` (B-C11) over random overlapping placements.

Each seed (0..249): an outline 20-80 mm, 0-3 footprint keepouts, 0-3 locked parts and 0-2
unlocked but non-movable parts placed legally, 2-30 movable parts (courtyards 0.5-8 mm)
dropped at random spots of the outline (overlapping each other, keepouts and the fixed
parts) — half of them already on the board, half moved there from the staging area by a
`Move`. `max_shift` is None or 0.2-5 mm. Every case is computed once and shared."""

from __future__ import annotations

import functools
import math
import random
from dataclasses import dataclass

from copperchat.board.geom import rect_poly
from copperchat.board.model import BoardModel, Point
from copperchat.board.placer import DoesNotFit, Move, apply_moves, edge_margin, legalize
from copperchat.board.placer.layout import box_of

from .builders import board, outline, zone
from .test_placer_props import Box, _legal_spot, gap, overlap_area, random_part

SEEDS = range(250)
GRID = 0.05
SNAP_TOL = GRID * math.sqrt(2) / 2 + 1e-6  # the start position is snapped to the grid first


@dataclass(frozen=True)
class LCase:
    seed: int
    model: BoardModel
    moves: tuple[Move, ...]
    movable: frozenset[str]
    locked: tuple[str, ...]
    keepouts: tuple[Box, ...]
    inner: Box
    max_shift: float | None
    start: dict[str, Point]
    result: tuple[Move, ...] | DoesNotFit


@functools.cache
def case(seed: int) -> LCase:
    rnd = random.Random(10_000 + seed)
    ow, oh = round(rnd.uniform(20, 80), 1), round(rnd.uniform(20, 80), 1)
    ox, oy = round(rnd.uniform(-10, 10), 1), round(rnd.uniform(-10, 10), 1)
    rules: list[tuple[str, float]] = [("min_clearance", 0.127)]
    if rnd.random() < 0.3:
        rules.append(("copper_edge_clearance", rnd.choice((0.2, 0.5, 1.0))))
    if rnd.random() < 0.3:
        rules.append(("courtyard_clearance", rnd.choice((0.0, 0.1, 0.25))))
    probe = board(parts=(), outline=outline(ox, oy, ow, oh), rules=tuple(rules))
    margin = edge_margin(probe)
    inner = (ox + margin, oy + margin, ox + ow - margin, oy + oh - margin)
    nets = [f"N{i}" for i in range(6)] + ["GND"]

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

    parts = []
    locked: list[str] = []
    fixed: list[str] = []
    taken: list[Box] = list(keep)
    for i in range(rnd.randint(0, 3)):
        q = _legal_spot(rnd, random_part(rnd, f"X{i + 1}", (0, 0), nets, locked=True), inner, taken)
        if q is not None:
            parts.append(q)
            locked.append(q.ref)
            taken.append(box_of(q))
    for i in range(rnd.randint(0, 2)):
        q = _legal_spot(rnd, random_part(rnd, f"F{i + 1}", (0, 0), nets), inner, taken)
        if q is not None:
            parts.append(q)
            fixed.append(q.ref)
            taken.append(box_of(q))

    moves: list[Move] = []
    movable: list[str] = []
    start: dict[str, Point] = {}
    sy = oy
    for i in range(rnd.randint(2, 30)):
        ref = f"P{i + 1}"
        x = rnd.uniform(inner[0] + 1, inner[2] - 1)
        y = rnd.uniform(inner[1] + 1, inner[3] - 1)
        if rnd.random() < 0.5:
            parts.append(random_part(rnd, ref, (x, y), nets))
        else:  # waits in the staging area, moved onto the board by a Move
            parts.append(random_part(rnd, ref, (ox + ow + 10.0, sy + 4.0), nets))
            sy += 10.0
            moves.append(Move(ref, round(x, 3), round(y, 3), 0.0, "top"))
            x, y = round(x, 3), round(y, 3)
        movable.append(ref)
        start[ref] = (x, y)
    # locked parts named movable never move
    movable.extend(r for r in locked if rnd.random() < 0.5)
    max_shift = None if rnd.random() < 0.5 else round(rnd.uniform(0.2, 5.0), 2)
    m = board(parts=tuple(parts), outline=outline(ox, oy, ow, oh), rules=tuple(rules), zones=zones)
    result = legalize(
        m,
        moves,
        movable=frozenset(movable),
        max_shift=max_shift,
        keepouts=[z.outline for z in zones],
        edge_margin=margin,
    )
    return LCase(
        seed,
        m,
        tuple(moves),
        frozenset(movable),
        tuple(locked),
        tuple(keep),
        inner,
        max_shift,
        start,
        result,
    )


def legal_cases() -> list[tuple[LCase, tuple[Move, ...], BoardModel]]:
    out = []
    for s in SEEDS:
        c = case(s)
        if not isinstance(c.result, DoesNotFit):
            out.append((c, c.result, apply_moves(apply_moves(c.model, c.moves), c.result)))
    return out


def _clearance(m: BoardModel) -> float:
    return max(dict(m.rules).get("courtyard_clearance", 0.0), 0.0)


def test_returns_every_movable_unlocked_part() -> None:
    for c, res, _ in legal_cases():
        want = sorted(r for r in c.movable if r not in c.locked)
        assert sorted(mv.ref for mv in res) == want, f"seed {c.seed}"


def test_no_overlaps() -> None:
    bad = []
    for c, _, after in legal_cases():
        o = after.outline
        sep = _clearance(after)
        parts = [p for p in after.parts if box_of(p)[0] < o.x + o.w]  # not the staging area
        for i, a in enumerate(parts):
            for b in parts[i + 1 :]:
                if a.side == b.side and gap(box_of(a), box_of(b)) < sep - 1e-6:
                    bad.append((c.seed, a.ref, b.ref))
    assert not bad, f"overlaps (seed, ref, ref): {bad[:10]}"


def test_inside_outline_and_out_of_keepouts() -> None:
    bad = []
    for c, res, after in legal_cases():
        i = c.inner
        for mv in res:
            b = box_of(after.part(mv.ref))
            if b[0] < i[0] - 1e-6 or b[1] < i[1] - 1e-6 or b[2] > i[2] + 1e-6 or b[3] > i[3] + 1e-6:
                bad.append((c.seed, mv.ref, "outline"))
            if any(overlap_area(b, k) > 1e-6 for k in c.keepouts):
                bad.append((c.seed, mv.ref, "keepout"))
    assert not bad, f"violations (seed, ref, what): {bad[:10]}"


def test_locks_unchanged() -> None:
    bad = []
    for c, res, after in legal_cases():
        moved = {mv.ref for mv in res}
        for r in c.locked:
            p0, p1 = c.model.part(r), after.part(r)
            if r in moved or (p0.x, p0.y, p0.rot) != (p1.x, p1.y, p1.rot):
                bad.append((c.seed, r))
    assert not bad, f"locked parts moved (seed, ref): {bad}"


def test_shift_within_max_shift() -> None:
    bad = []
    n_limited = 0
    for c, res, _ in legal_cases():
        if c.max_shift is None:
            continue
        n_limited += 1
        for mv in res:
            d = math.dist(c.start[mv.ref], (mv.x, mv.y))
            if d > c.max_shift + SNAP_TOL:
                bad.append((c.seed, mv.ref, round(d, 3), c.max_shift))
    assert n_limited >= 10, n_limited
    assert not bad, f"shift > max_shift (seed, ref, shift, max): {bad[:10]}"


def test_grid_snap_005() -> None:
    bad = []
    for c, res, _ in legal_cases():
        for mv in res:
            for v in (mv.x, mv.y):
                if abs(v / GRID - round(v / GRID)) * GRID > 1e-6:
                    bad.append((c.seed, mv))
    assert not bad, f"off-grid moves (seed, move): {bad[:10]}"


def test_does_not_fit_names_parts() -> None:
    for s in SEEDS:
        r = case(s).result
        if isinstance(r, DoesNotFit):
            c = case(s)
            assert r.refs, f"seed {s}: {r}"
            assert set(r.refs) <= c.movable - set(c.locked), f"seed {s}: {r}"


def test_sparse_without_max_shift_always_legal() -> None:
    """Σ courtyards + keepouts ≤ 25 % of the outline and no `max_shift` → never
    `DoesNotFit` (the push-apart converges). Not required by B-C11 (DoesNotFit is a legal
    answer of the legaliser); records a quality limit."""
    n = 0
    bad = []
    for s in SEEDS:
        c = case(s)
        o = c.model.outline
        area = sum(
            (b[2] - b[0]) * (b[3] - b[1])
            for b in (box_of(p) for p in apply_moves(c.model, c.moves).parts)
        )
        area += sum((k[2] - k[0]) * (k[3] - k[1]) for k in c.keepouts)
        if c.max_shift is not None or area > 0.25 * o.w * o.h:
            continue
        n += 1
        if isinstance(c.result, DoesNotFit):
            bad.append((s, c.result))
    print(f"sparse seeds without max_shift: {n}")
    assert n >= 25, n
    assert not bad, f"DoesNotFit on sparse boards (seed, result): {bad[:5]}"


def test_mix_of_outcomes() -> None:
    """Sanity: the generator produces both legal results and (dense / tight max_shift)
    DoesNotFit."""
    ok = sum(not isinstance(case(s).result, DoesNotFit) for s in SEEDS)
    assert len(SEEDS) // 3 <= ok < len(SEEDS), ok
