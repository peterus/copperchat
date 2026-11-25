"""Placer regressions from the PR #108 review (round 1): a legal board is never rewritten,
the edge strip fits the block it is made for, dense boards answer quickly."""

from __future__ import annotations

import time

from copperchat.board.classify import classify
from copperchat.board.jobs import edge_strip
from copperchat.board.model import Rect
from copperchat.board.placer import DoesNotFit, legalize, place_block

from .builders import outline, part
from .builders_circuit import circuit

TOP = frozenset({"top"})


def test_legal_off_grid_board_unchanged() -> None:
    # off the 0.05 mm grid and legal: legalize returns the exact positions
    m = circuit(part("R1", 3.81, 10.07, half=1.0), part("R2", 13.33, 10.0, half=1.0))
    out = legalize(
        m, (), movable=frozenset({"R1", "R2"}), max_shift=None, keepouts=(), edge_margin=0.3
    )
    assert not isinstance(out, DoesNotFit)
    assert {(mv.ref, mv.x, mv.y) for mv in out} == {("R1", 3.81, 10.07), ("R2", 13.33, 10.0)}


def test_max_shift_from_true_start() -> None:
    # R1 overlaps the fixed R2 by 0.12 mm: more than max_shift 0.01 + the grid snap
    m = circuit(part("R1", 10.02, 10.0, half=1.0), part("R2", 11.9, 10.0, half=1.0))
    out = legalize(m, (), movable=frozenset({"R1"}), max_shift=0.01, keepouts=(), edge_margin=0.3)
    assert isinstance(out, DoesNotFit)


def test_conflict_leaves_legal_neighbours_exact() -> None:
    m = circuit(
        part("R1", 10.02, 10.0, half=1.0),
        part("R2", 11.5, 10.0, half=1.0),
        part("R3", 30.013, 20.007, half=1.0),
    )
    out = legalize(
        m, (), movable=frozenset({"R1", "R2", "R3"}), max_shift=None, keepouts=(), edge_margin=0.3
    )
    assert not isinstance(out, DoesNotFit)
    r3 = next(mv for mv in out if mv.ref == "R3")
    assert (r3.x, r3.y) == (30.013, 20.007)


def test_shift_of_exactly_max_shift_allowed() -> None:
    # R1 must move 0.5 mm (+ the grid snap of its 10.02 start): allowed with max_shift 0.5
    m = circuit(part("R1", 10.02, 10.0, half=1.0), part("R2", 10.02, 11.5, half=1.0))
    out = legalize(m, (), movable=frozenset({"R1"}), max_shift=0.5, keepouts=(), edge_margin=0.3)
    assert not isinstance(out, DoesNotFit)


def test_edge_strip_fits_a_single_big_part() -> None:
    for kc in (0.0, 0.1, 0.2, 0.5):
        m = circuit(part("J1", 150.0, 40.0, half=4.0, lib_id="Connector:J"))
        cls = classify(m)
        region, target = edge_strip(m, cls, ("J1",), "left", kc, None)
        r = place_block(
            m,
            cls,
            ["J1"],
            region,
            side="top",
            rotation=None,
            keep_channels=kc,
            allowed_sides=TOP,
            edge="left",
            target=target,
        )
        assert not isinstance(r, DoesNotFit), (kc, r)


def _dense(n: int, size: float, *, half: float) -> list:  # type: ignore[type-arg]
    """`n` parts on a square grid spread over the whole board (pitch decides overlaps)."""
    side = round(n**0.5)
    pitch = (size - 2 * half - 1) / (side - 1)
    return [
        part(
            f"R{i + 1}",
            0.5 + half + (i % side) * pitch,
            0.5 + half + (i // side) * pitch,
            half=half,
        )
        for i in range(side * side)
    ]


def test_legalize_dense_board_answers_quickly() -> None:
    parts = _dense(289, 60.0, half=1.8)  # pitch 3.3 < 3.6: every neighbour overlaps
    m = circuit(*parts, outline=outline(0, 0, 60, 60))
    t = time.monotonic()
    legalize(
        m,
        (),
        movable=frozenset(p.ref for p in parts),
        max_shift=None,
        keepouts=(),
        edge_margin=0.3,
    )
    assert time.monotonic() - t < 30.0


def test_place_block_does_not_fit_quickly() -> None:
    parts = _dense(400, 150.0, half=1.4)  # 7.5 mm pitch: no 12 mm hole anywhere
    big = part("U1", 500.0, 500.0, half=6.0)
    m = circuit(*parts, big, outline=outline(0, 0, 150, 150))
    t = time.monotonic()
    r = place_block(
        m,
        classify(m),
        ["U1"],
        Rect(0, 0, 150, 150),
        side="top",
        rotation=None,
        allowed_sides=TOP,
    )
    assert isinstance(r, DoesNotFit)
    assert time.monotonic() - t < 15.0


def test_tight_hole_found_on_a_big_board() -> None:
    # a 5 x 5 mm hole (one 4 x 4 part + 2 x 0.5 channel) in an otherwise full 100 mm board
    obstacles = []
    k = 0
    for i in range(20):
        for j in range(20):
            x0, y0 = 1.0 + i * 4.9, 1.0 + j * 4.9
            if (i, j) == (8, 8):
                continue
            k += 1
            obstacles.append(part(f"R{k}", x0 + 2.2, y0 + 2.2, half=2.2))
    big = part("U1", 500.0, 500.0, half=2.0)
    m = circuit(*obstacles, big, outline=outline(0, 0, 100, 100))
    r = place_block(
        m,
        classify(m),
        ["U1"],
        Rect(0, 0, 100, 100),
        side="top",
        rotation=None,
        keep_channels=0.25,
        allowed_sides=TOP,
    )
    assert not isinstance(r, DoesNotFit), r


def test_block_with_many_outside_pads_is_fast() -> None:
    # 200 outside parts with pads on the block's nets: the cost uses a nearest index
    from .builders_circuit import cap, ic

    outside = [
        cap(f"C{i + 1}", f"N{i % 20}", "+3V3", at=(5 + (i % 20) * 7, 5 + (i // 20) * 9))
        for i in range(200)
    ]
    u = ic("U1", {str(n + 1): f"N{n % 20}" for n in range(40)}, at=(500.0, 500.0), pitch=0.5)
    m = circuit(*outside, u, outline=outline(0, 0, 150, 150))
    t = time.monotonic()
    place_block(
        m,
        classify(m),
        ["U1"],
        Rect(0, 0, 150, 150),
        side="top",
        rotation=None,
        allowed_sides=TOP,
    )
    assert time.monotonic() - t < 15.0


def test_stacked_obstacles_counted_once() -> None:
    stack = [part(f"R{i + 1}", 5.0, 5.0, half=2.0) for i in range(12)]  # all on one spot
    big = part("U1", 500.0, 500.0, half=2.0)
    m = circuit(*stack, big, outline=outline(0, 0, 14, 14))
    r = place_block(
        m,
        classify(m),
        ["U1"],
        Rect(0, 0, 14, 14),
        side="top",
        rotation=None,
        allowed_sides=TOP,
    )
    assert not isinstance(r, DoesNotFit), r


def test_nearest_index_matches_brute_force() -> None:
    import random

    from copperchat.board.geom import octi_dist
    from copperchat.board.placer.pack import _Nearest

    rnd = random.Random(7)
    for _ in range(20):
        pts = [(rnd.uniform(-50, 150), rnd.uniform(-50, 150)) for _ in range(rnd.randint(33, 300))]
        idx = _Nearest(pts)
        for _ in range(50):
            q = (rnd.uniform(-300, 400), rnd.uniform(-300, 400))
            assert abs(idx.dist(q) - min(octi_dist(q, p) for p in pts)) < 1e-9
