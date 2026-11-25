from __future__ import annotations

import subprocess
import sys
from dataclasses import replace

import pytest

from copperchat.board.geom import rect_poly
from copperchat.board.model import Outline

from .builders import board, outline, part


def test_no_kipy_import() -> None:
    code = (
        "import sys, copperchat.board.model, copperchat.board.geom;assert 'kipy' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_part_missing_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        board().part("X")


def test_part_lookup() -> None:
    assert board().part("R1").ref == "R1"


def test_placed_fully_inside() -> None:
    m = board()
    assert m.placed(part("A", 50, 40)) is True


def test_placed_false_when_crossing_by_001() -> None:
    m = board()
    # courtyard half=2 -> left edge at x - 2; outline starts at 0
    assert m.placed(part("A", 2.0, 40)) is True
    assert m.placed(part("A", 1.99, 40)) is False


def test_placed_false_without_outline() -> None:
    none = Outline(kind="none", x=0, y=0, w=0, h=0, corner_radius=0.0, polygon=())
    m = replace(board(), outline=none)
    assert m.placed(part("A", 50, 40)) is False


def test_placed_false_without_courtyard() -> None:
    assert board().placed(part("A", 50, 40, courtyard=())) is False


def test_placed_other_outline_uses_polygon() -> None:
    poly = ((0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (10.0, 20.0), (10.0, 10.0), (0.0, 10.0))
    o = Outline(kind="other", x=0, y=0, w=20, h=20, corner_radius=0.0, polygon=poly)
    m = replace(board(), outline=o)
    assert m.placed(part("A", 5, 5)) is True
    assert m.placed(part("A", 5, 15)) is False  # in the notch
    assert outline().kind == "rect"


def test_placed_rounded_rect_outline() -> None:
    sq = tuple(rect_poly(0, 0, 100, 80))
    o = Outline(kind="rect", x=0, y=0, w=100, h=80, corner_radius=10.0, polygon=sq)
    m = replace(board(), outline=o)
    assert m.placed(part("A", 50, 40)) is True
    assert m.placed(part("A", 2.5, 2.5)) is False  # corner cut by the radius
