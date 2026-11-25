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
    # an overlapping pair; the shift is measured from the unsnapped start
    m = circuit(part("R1", 10.01, 10.0, half=1.0), part("R2", 11.0, 10.0, half=1.0))
    out = legalize(m, (), movable=frozenset({"R1"}), max_shift=0.5, keepouts=(), edge_margin=0.3)
    assert isinstance(out, DoesNotFit)  # R1 must move ~1 mm > 0.5


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
