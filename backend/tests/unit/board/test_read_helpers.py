"""Pure helpers of the board loader (outline, arcs, footprint frame, layer types)."""

from __future__ import annotations

import math

import pytest
from kipy.board_types import BoardArc, BoardRectangle, BoardSegment
from kipy.geometry import Vector2
from kipy.proto.board.board_types_pb2 import BoardLayer

from copperchat.board.geom import polygon_area
from copperchat.kicad_tools.board.read import _arc_points, _layer_types, _outline, _to_board


def _v(x: float, y: float) -> Vector2:
    return Vector2.from_xy_mm(x, y)


def _seg(a: tuple[float, float], b: tuple[float, float]) -> BoardSegment:
    s = BoardSegment()
    s.layer = BoardLayer.BL_Edge_Cuts
    s.start, s.end = _v(*a), _v(*b)
    return s


def test_outline_none() -> None:
    assert _outline([]).kind == "none"


def test_outline_single_rectangle() -> None:
    r = BoardRectangle()
    r.layer = BoardLayer.BL_Edge_Cuts
    r.top_left, r.bottom_right = _v(10, 20), _v(60, 50)
    r.proto.shape.rectangle.corner_radius.value_nm = 2_000_000
    o = _outline([r])
    assert (o.kind, o.x, o.y, o.w, o.h, o.corner_radius) == ("rect", 10, 20, 50, 30, 2)
    assert len(o.polygon) > 4


def test_outline_segments_in_any_order_and_direction() -> None:
    segs = [
        _seg((0, 0), (40, 0)),
        _seg((40, 30), (0, 30)),
        _seg((0, 0), (0, 30)),
        _seg((40, 0), (40, 30)),
    ]
    o = _outline(segs)
    assert (o.kind, o.x, o.y, o.w, o.h) == ("other", 0, 0, 40, 30)
    assert abs(polygon_area(o.polygon)) == pytest.approx(1200)


def test_outline_with_arc_corner_and_cutout_takes_outer_loop() -> None:
    arc = BoardArc()
    arc.layer = BoardLayer.BL_Edge_Cuts
    c = 5 * (1 - math.sqrt(0.5))
    arc.start, arc.mid, arc.end = _v(35, 0), _v(40 - c, c), _v(40, 5)
    outer = [
        _seg((0, 0), (35, 0)),
        arc,
        _seg((40, 5), (40, 30)),
        _seg((40, 30), (0, 30)),
        _seg((0, 30), (0, 0)),
    ]
    hole = [
        _seg((10, 10), (20, 10)),
        _seg((20, 10), (20, 20)),
        _seg((20, 20), (10, 20)),
        _seg((10, 20), (10, 10)),
    ]
    o = _outline(hole + outer)
    assert o.kind == "other"
    area = abs(polygon_area(o.polygon))
    assert 1200 - 25 < area < 1200  # the rounded corner cuts a little off


def test_outline_open_chain_falls_back_to_hull() -> None:
    o = _outline([_seg((0, 0), (10, 0)), _seg((10, 0), (10, 10))])
    assert o.kind == "other" and len(o.polygon) == 3


def test_arc_points_lie_on_the_circle() -> None:
    pts = _arc_points((0.0, 0.0), (1.0, 1.0), (2.0, 0.0))
    assert pts[0] == (0.0, 0.0) and pts[-1] == (2.0, 0.0)
    assert all(math.hypot(x - 1, y) == pytest.approx(1) for x, y in pts)
    assert all(y >= -1e-9 for _, y in pts)  # through the mid point, not the other way


def test_footprint_frame_to_board() -> None:
    # Measured on the pinned server: DWM1000 at (150, 60), keepout corner (-8.89, -1.32)
    # in the footprint frame; rotated 90° the keepout reaches y = 68.89.
    assert _to_board((-8.89, -1.32), (150.0, 60.0), 0.0) == (141.11, 58.68)
    assert _to_board((-8.89, -1.32), (150.0, 60.0), 90.0) == (148.68, 68.89)


def test_layer_types_from_header() -> None:
    text = (
        '(kicad_pcb\n\t(version 1)\n\t(layers\n\t\t(0 "F.Cu" signal "top_cu")\n'
        '\t\t(4 "In1.Cu" power)\n\t\t(2 "B.Cu" mixed)\n\t\t(5 "F.SilkS" user "F.Silkscreen")\n'
        '\t)\n\t(setup\n\t\t(0 "X.Cu" jumper)\n\t)\n)'
    )
    assert _layer_types(text, ("F.Cu", "In1.Cu", "B.Cu")) == (
        ("F.Cu", "signal"),
        ("In1.Cu", "power"),
        ("B.Cu", "mixed"),
    )
    assert _layer_types("(kicad_pcb)", ("F.Cu",)) == ()
