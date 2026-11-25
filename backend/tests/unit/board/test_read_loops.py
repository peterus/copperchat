"""`read._loops`: Edge.Cuts pieces chained into closed loops (endpoint index)."""

from __future__ import annotations

import math
import random
import time

from kipy.board_types import BoardSegment
from kipy.geometry import Vector2

from copperchat.kicad_tools.board import read

Pt = tuple[float, float]


def _seg(a: Pt, b: Pt) -> BoardSegment:
    s = BoardSegment()
    s.start = Vector2.from_xy(round(a[0] * 1e6), round(a[1] * 1e6))
    s.end = Vector2.from_xy(round(b[0] * 1e6), round(b[1] * 1e6))
    return s


def _loops_linear(pieces: list[list[Pt]]) -> list[list[Pt]]:
    """The pre-index chaining (pop(0) + linear scan), the reference behaviour."""
    loops: list[list[Pt]] = []
    pieces = [list(p) for p in pieces]
    while pieces:
        chain = pieces.pop(0)
        progress = True
        while progress and not (len(chain) > 2 and read._close(chain[0], chain[-1])):
            progress = False
            for i, piece in enumerate(pieces):
                if read._close(chain[-1], piece[0]):
                    chain += piece[1:]
                elif read._close(chain[-1], piece[-1]):
                    chain += piece[-2::-1]
                else:
                    continue
                pieces.pop(i)
                progress = True
                break
        if len(chain) > 2 and read._close(chain[0], chain[-1]):
            loops.append(chain[:-1])
    return loops


def _ring(n: int, r: float, cx: float = 0.0) -> list[tuple[Pt, Pt]]:
    pts = [
        (
            round(cx + r * math.cos(2 * math.pi * i / n), 6),
            round(r * math.sin(2 * math.pi * i / n), 6),
        )
        for i in range(n)
    ]
    return [(pts[i], pts[(i + 1) % n]) for i in range(n)]


def test_large_outline_chains_fast() -> None:
    rng = random.Random(3)
    ends = _ring(5000, 50.0)
    rng.shuffle(ends)
    segs = [_seg(a, b) if rng.random() < 0.5 else _seg(b, a) for a, b in ends]
    t0 = time.perf_counter()
    loops = read._loops(segs)
    assert time.perf_counter() - t0 < 0.5
    assert len(loops) == 1 and len(loops[0]) == 5000


def test_matches_linear_chaining() -> None:
    """Same loops as the linear scan: several loops, gaps, branches, jitter < 1 um."""
    rng = random.Random(5)
    for trial in range(300):
        ends: list[tuple[Pt, Pt]] = []
        for k in range(rng.randint(1, 3)):
            ends += _ring(rng.randint(3, 10), rng.uniform(0.01, 5.0), cx=20.0 * k)
        if trial % 5 == 1:
            ends.append(ends[rng.randrange(len(ends))])  # a duplicate (branch)
        if trial % 7 == 2:
            a, b = ends[0]
            ends[0] = (a, (b[0] + 0.01, b[1]))  # a gap
        if trial % 3 == 0:
            ends = [((a[0] + rng.uniform(-4e-4, 4e-4), a[1]), b) for a, b in ends]
        rng.shuffle(ends)
        segs = [_seg(a, b) if rng.random() < 0.5 else _seg(b, a) for a, b in ends]
        pieces = [read._dense(s)[0] for s in segs]
        assert read._loops(segs) == _loops_linear(pieces), trial
