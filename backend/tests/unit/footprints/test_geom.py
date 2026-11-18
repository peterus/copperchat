"""Geometry primitives for fp_check (FP-C3)."""

from __future__ import annotations

import math
import random

import pytest

from copperchat.kicad_tools.footprints import geom
from copperchat.kicad_tools.footprints.model import Pad, Shape


def _pad(shape: str = "rect", at=(-1.1, 0.0), size=(1.3, 3.5), rot=0.0, ratio=None) -> Pad:
    return Pad("1", "smd", shape, at, rot, size, ratio, None, ("F.Cu",))


def _seg(a, b, w=0.12) -> Shape:
    return Shape("segment", "F.SilkS", (a, b), None, None, w, False)


def test_rect_pad_vertices() -> None:
    poly = geom.pad_polygon(_pad())
    assert len(poly) == 4
    flat = [c for p in sorted(poly) for c in p]
    want = [
        c for p in sorted([(-1.75, -1.75), (-0.45, -1.75), (-0.45, 1.75), (-1.75, 1.75)]) for c in p
    ]
    assert flat == pytest.approx(want)


def test_rotated_pad_swaps_extents() -> None:
    poly = geom.pad_polygon(_pad(rot=90.0))
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    assert max(xs) - min(xs) == pytest.approx(3.5)
    assert max(ys) - min(ys) == pytest.approx(1.3)
    assert (min(xs) + max(xs)) / 2 == pytest.approx(-1.1)


def test_roundrect_corner_radius() -> None:
    poly = geom.pad_polygon(_pad("roundrect", at=(0, 0), size=(2.0, 1.0), ratio=0.25))
    r = 0.25 * 1.0
    # no vertex in the bounding corner; the corner arc is r from its centre
    assert (1.0, 0.5) not in poly
    assert min(math.hypot(x - 1.0, y - 0.5) for x, y in poly) == pytest.approx(
        r * (math.sqrt(2) - 1), abs=1e-3
    )
    assert max(p[0] for p in poly) == pytest.approx(1.0)


def test_circle_pad_geometry_and_polygon() -> None:
    g = geom.pad_geometry(_pad("circle", at=(1, 2), size=(1.0, 1.0)))
    assert geom.is_circle(g) and g == ("circle", (1, 2), 0.5)
    assert len(geom.pad_polygon(_pad("circle", size=(1.0, 1.0)))) == 64


def test_distance_l201_pads() -> None:
    a = geom.pad_polygon(_pad(at=(-1.1, 0.0)))
    b = geom.pad_polygon(_pad(at=(1.1, 0.0)))
    assert geom.distance(a, b) == pytest.approx(0.9)


def test_distance_overlap_and_containment() -> None:
    a = geom.pad_polygon(_pad(at=(0, 0), size=(2, 2)))
    assert geom.distance(a, geom.pad_polygon(_pad(at=(1, 0), size=(2, 2)))) == 0.0
    assert geom.distance(a, geom.pad_polygon(_pad(at=(0, 0), size=(0.5, 0.5)))) == 0.0
    assert geom.distance(a, geom.Seg((-0.1, 0), (0.1, 0), 0.05)) == 0.0


def test_distance_seg_and_circle() -> None:
    a = geom.pad_polygon(_pad(at=(0, 0), size=(2, 2)))
    assert geom.distance(a, geom.Seg((2, -5), (2, 5), 0.1)) == pytest.approx(0.9)
    assert geom.distance(a, ("circle", (3.0, 0.0), 0.5)) == pytest.approx(1.5)
    assert geom.distance(("circle", (0.0, 0.0), 1.0), ("circle", (4.0, 0.0), 1.0)) == pytest.approx(
        2.0
    )
    assert geom.distance(
        geom.Seg((0, 0), (1, 0), 0.1), geom.Seg((0, 1), (1, 1), 0.2)
    ) == pytest.approx(0.7)
    assert geom.distance(geom.Seg((0, 0), (2, 2)), geom.Seg((0, 2), (2, 0))) == 0.0


def test_point_in_polygon_boundary_inside() -> None:
    sq = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    assert geom.point_in_polygon((0.5, 0.5), sq)
    assert geom.point_in_polygon((1.0, 0.5), sq)
    assert geom.point_in_polygon((0.0, 0.0), sq)
    assert not geom.point_in_polygon((1.1, 0.5), sq)


def test_stroke_segments_arc_and_circle() -> None:
    arc = Shape("arc", "F.SilkS", ((1, 0), (0, 1), (-1, 0)), None, None, 0.2, False)
    segs = geom.stroke_segments(arc)
    assert len(segs) == 32 and segs[0].half_width == pytest.approx(0.1)
    assert all(math.hypot(*s.a) == pytest.approx(1.0) for s in segs)
    assert all(s.a[1] >= -1e-9 for s in segs)  # goes through mid (0, 1)
    circ = Shape("circle", "F.SilkS", (), (0, 0), (2.0, 2.0), 0.1, True)
    assert len(geom.stroke_segments(circ)) == 64


def test_closed_outline() -> None:
    sq = [
        _seg((0, 0), (1, 0)),
        _seg((1, 1), (1, 0)),
        _seg((1, 1), (0, 1)),
        _seg((0, 1), (0, 0.0005)),
    ]
    poly = geom.closed_outline(sq)
    assert poly is not None and len(poly) == 4
    assert (
        geom.closed_outline([_seg((0, 0), (1, 0)), _seg((1, 0), (1, 1)), _seg((1, 1), (0, 0.5))])
        is None
    )
    rect = Shape("rect", "F.CrtYd", ((0, 0), (2, 1)), (1, 0.5), (2, 1), 0.05, True)
    assert geom.closed_outline([rect]) == [(0, 0), (2, 0), (2, 1), (0, 1)]
    assert geom.closed_outline([]) is None


def test_candidate_pairs_matches_brute_force() -> None:
    rnd = random.Random(7)
    boxes = []
    for _ in range(1000):
        x, y = rnd.uniform(0, 100), rnd.uniform(0, 100)
        boxes.append((x, y, x + rnd.uniform(0, 3), y + rnd.uniform(0, 3)))
    gap = 0.5
    brute = {
        (i, j)
        for i in range(len(boxes))
        for j in range(i + 1, len(boxes))
        if boxes[i][0] - gap <= boxes[j][2]
        and boxes[j][0] - gap <= boxes[i][2]
        and boxes[i][1] - gap <= boxes[j][3]
        and boxes[j][1] - gap <= boxes[i][3]
    }
    got = list(geom.candidate_pairs(boxes, gap))
    assert len(got) == len(set(got))
    assert set(got) == brute
