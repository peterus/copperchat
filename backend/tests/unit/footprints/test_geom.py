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


def _xpad(shape: str, size, extra: dict, at=(0.0, 0.0), rot=0.0) -> Pad:
    return Pad(
        "1", "smd", shape, at, rot, size, None, None, ("F.Cu",), tuple(sorted(extra.items()))
    )


def _flat(pts) -> list[float]:
    return [c for p in pts for c in p]


def test_trapezoid_exact_corners() -> None:
    pad = _xpad("trapezoid", (2.0, 1.0), {"trapezoid_delta": (0.0, 0.4)})
    want = [(-1.2, 0.5), (-0.8, -0.5), (0.8, -0.5), (1.2, 0.5)]
    assert _flat(geom.pad_polygon(pad)) == pytest.approx(_flat(want))
    pad = _xpad("trapezoid", (2.0, 1.0), {"trapezoid_delta": (0.2, 0.0)})
    want = [(-1.0, 0.6), (-1.0, -0.6), (1.0, -0.4), (1.0, 0.4)]
    assert _flat(geom.pad_polygon(pad)) == pytest.approx(_flat(want))


def test_chamfered_rect_corner_cut() -> None:
    pad = _xpad(
        "chamfered_rect", (2.0, 1.0), {"chamfer_ratio": 0.2, "chamfered_corners": ("top_left",)}
    )
    poly = geom.pad_polygon(pad)
    assert len(poly) == 5
    assert (-1.0, -0.5) not in poly
    assert (-0.8, -0.5) in [(round(x, 9), round(y, 9)) for x, y in poly]
    assert (-1.0, -0.3) in [(round(x, 9), round(y, 9)) for x, y in poly]
    assert (1.0, 0.5) in poly


def test_custom_pad_parts() -> None:
    sq = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    prim = {"kind": "polygon", "points": sq, "width": 0.0, "closed": True, "filled": True}
    seg = {"kind": "segment", "points": [(0.0, 0.0), (0.0, 2.0)], "width": 0.2}
    pad = _xpad(
        "custom", (0.2, 0.2), {"anchor": "circle", "primitives": (prim, seg)}, at=(5.0, 5.0)
    )
    parts = geom.pad_parts(pad)
    assert len(parts) == 3
    assert geom.is_circle(parts[0]) and parts[0] == ("circle", (5.0, 5.0), 0.1)
    assert _flat(parts[1]) == pytest.approx(_flat([(5 + x, 5 + y) for x, y in sq]))
    assert parts[2] == geom.Seg((5.0, 5.0), (5.0, 7.0), 0.1)
    assert geom.pad_parts(_pad())[0] == geom.pad_geometry(_pad())


def test_offset_applied_before_rotation() -> None:
    pad = _xpad("rect", (2.0, 1.0), {"offset": (1.0, 0.0)}, at=(10.0, 10.0), rot=90.0)
    poly = geom.pad_polygon(pad)
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    # KiCad +90 deg is counter-clockwise on screen: local +x -> screen up (y down => -y)
    assert (min(xs) + max(xs)) / 2 == pytest.approx(10.0)
    assert (min(ys) + max(ys)) / 2 == pytest.approx(9.0)
    c = _xpad("circle", (1.0, 1.0), {"offset": (1.0, 0.0)}, at=(10.0, 10.0), rot=90.0)
    g = geom.pad_geometry(c)
    assert geom.is_circle(g) and g[1] == pytest.approx((10.0, 9.0))


def test_rotation_sign_30_degrees() -> None:
    pad = _pad("rect", at=(0.0, 0.0), size=(2.0, 0.0001), rot=30.0)
    xs = [p for p in geom.pad_polygon(pad)]
    right = max(xs)
    # local +x end rotates counter-clockwise on screen: up, i.e. negative y
    assert right[0] == pytest.approx(math.cos(math.radians(30)), abs=1e-3)
    assert right[1] == pytest.approx(-0.5, abs=1e-3)


def test_oval_stadium() -> None:
    poly = geom.pad_polygon(_pad("oval", at=(0, 0), size=(3.0, 1.0)))
    assert max(p[0] for p in poly) == pytest.approx(1.5)
    assert max(p[1] for p in poly) == pytest.approx(0.5)
    assert geom.point_in_polygon((0.0, 0.4), poly)
    assert not geom.point_in_polygon((1.45, 0.4), poly)  # outside the rounded end
