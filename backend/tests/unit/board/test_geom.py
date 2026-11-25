from __future__ import annotations

import math

import pytest

from copperchat.board.geom import (
    convex_hull,
    octi_dist,
    point_in_polygon,
    polygon_area,
    polygon_inside,
    polygons_overlap,
    rect_poly,
)

L_SHAPE = [(0.0, 0.0), (10.0, 0.0), (10.0, 4.0), (4.0, 4.0), (4.0, 10.0), (0.0, 10.0)]
U_SHAPE = [
    (0.0, 0.0),
    (10.0, 0.0),
    (10.0, 10.0),
    (7.0, 10.0),
    (7.0, 3.0),
    (3.0, 3.0),
    (3.0, 10.0),
    (0.0, 10.0),
]


def test_octi_dist() -> None:
    assert octi_dist((0, 0), (3, 4)) == pytest.approx(4 + 3 * (math.sqrt(2) - 1))
    assert octi_dist((0, 0), (5, 0)) == 5
    assert octi_dist((1, 1), (1, 1)) == 0


def test_polygon_area() -> None:
    assert polygon_area(rect_poly(0, 0, 10, 5)) == pytest.approx(50)
    assert polygon_area(list(reversed(rect_poly(0, 0, 10, 5)))) == pytest.approx(50)
    assert polygon_area(L_SHAPE) == pytest.approx(64)


def test_rect_poly_rounded() -> None:
    assert len(rect_poly(0, 0, 10, 5)) == 4
    assert len(rect_poly(0, 0, 10, 5, r=1.0)) == 4 * 9
    assert polygon_area(rect_poly(0, 0, 10, 5, r=1.0)) == pytest.approx(
        50 - (4 - math.pi), abs=0.05
    )


def test_point_in_polygon() -> None:
    sq = rect_poly(0, 0, 10, 10)
    assert point_in_polygon((5, 5), sq)
    assert point_in_polygon((0, 5), sq)  # edge
    assert point_in_polygon((0, 0), sq)  # corner
    assert not point_in_polygon((11, 5), sq)
    assert not point_in_polygon((7, 7), L_SHAPE)  # notch


def test_polygon_inside_convex_and_concave() -> None:
    assert polygon_inside(rect_poly(1, 1, 3, 3), rect_poly(0, 0, 10, 10))
    assert not polygon_inside(rect_poly(8, 8, 3, 3), rect_poly(0, 0, 10, 10))
    assert polygon_inside(rect_poly(1, 1, 2, 2), L_SHAPE)
    # all corners inside the L, but the box spans the notch
    assert not polygon_inside(rect_poly(1, 1, 8, 8), L_SHAPE)
    # all four corners inside the U, but the box spans its notch
    assert not polygon_inside(rect_poly(1, 6, 8, 2), U_SHAPE)
    assert polygon_inside(rect_poly(1, 1, 8, 1), U_SHAPE)


def test_polygons_overlap_convex_and_concave() -> None:
    assert polygons_overlap(rect_poly(0, 0, 5, 5), rect_poly(4, 4, 5, 5))
    assert not polygons_overlap(rect_poly(0, 0, 5, 5), rect_poly(6, 6, 2, 2))
    assert polygons_overlap(rect_poly(0, 0, 10, 10), rect_poly(2, 2, 1, 1))  # containment
    # box sitting in the notch of the L: no overlap
    assert not polygons_overlap(L_SHAPE, rect_poly(5, 5, 3, 3))
    assert polygons_overlap(L_SHAPE, rect_poly(3, 3, 3, 3))
    # an edge of the box crosses the L arm, no vertices inside each other
    assert polygons_overlap(L_SHAPE, rect_poly(-1, 1, 12, 1))


def test_convex_hull() -> None:
    pts = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0), (2.0, 2.0)]
    hull = convex_hull(pts)
    assert sorted(hull) == sorted(pts[:4])
    assert len(hull) == 4
