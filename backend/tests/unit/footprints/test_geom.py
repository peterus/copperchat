"""Geometry primitives for fp_check (FP-C3)."""

from __future__ import annotations

import itertools
import math
import random
import time

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


def test_custom_pad_primitives_follow_offset() -> None:
    """KiCad draws primitives at the shape position: pad position + rotated offset."""
    prim = {"kind": "rect", "points": [(0.0, 0.0), (1.0, 0.5)], "width": 0.0}
    circ = {"kind": "circle", "center": (0.0, 0.0), "size": (0.4, 0.4), "width": 0.0}
    seg = {"kind": "segment", "points": [(0.0, 0.0), (1.0, 0.0)], "width": 0.2}
    extra = {"anchor": "rect", "offset": (1.0, 0.0), "primitives": (prim, circ, seg)}
    pad = _xpad("custom", (0.2, 0.2), extra, at=(10.0, 10.0), rot=90.0)
    anchor, rect, disc, line = geom.pad_parts(pad)
    # +90 deg: local +x -> screen -y, local +y -> screen +x
    assert _flat(rect) == pytest.approx(_flat([(10, 9), (10, 8), (10.5, 8), (10.5, 9)]))
    assert geom.is_circle(disc) and disc[1] == pytest.approx((10.0, 9.0))
    assert isinstance(line, geom.Seg)
    assert line.a == pytest.approx((10.0, 9.0)) and line.b == pytest.approx((10.0, 8.0))
    xs = [x for x, _ in anchor]
    ys = [y for _, y in anchor]
    assert (sum(xs) / 4, sum(ys) / 4) == pytest.approx((10.0, 9.0))


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


def _closed_outline_linear(pieces: list[list[tuple[float, float]]]):
    """The pre-index chaining (linear scan), as the reference for the endpoint index."""

    def near(p, q) -> bool:
        return math.hypot(p[0] - q[0], p[1] - q[1]) <= geom.LOOP_TOL_MM

    pieces = [list(p) for p in pieces]
    path = list(pieces.pop(0))
    while pieces:
        for i, piece in enumerate(pieces):
            if near(path[-1], piece[0]):
                path += piece[1:]
            elif near(path[-1], piece[-1]):
                path += piece[-2::-1]
            else:
                continue
            del pieces[i]
            break
        else:
            return None
    if len(path) < 4 or not near(path[0], path[-1]):
        return None
    return path[:-1]


def _ring(n: int, r: float = 10.0, jitter: float = 0.0, rng=None):
    pts = [(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n)) for i in range(n)]

    def j(p):
        if not jitter:
            return p
        return (p[0] + rng.uniform(-jitter, jitter), p[1] + rng.uniform(-jitter, jitter))

    return [(j(pts[i]), j(pts[(i + 1) % n])) for i in range(n)]


def test_closed_outline_large_loop_fast() -> None:
    rng = random.Random(7)
    ends = _ring(5000)
    rng.shuffle(ends)
    segs = [_seg(a, b) if rng.random() < 0.5 else _seg(b, a) for a, b in ends]
    t0 = time.perf_counter()
    poly = geom.closed_outline(segs)
    assert time.perf_counter() - t0 < 1.0
    assert poly is not None and len(poly) == 5000


def test_closed_outline_matches_linear_scan() -> None:
    """Same result as the linear scan, also with endpoints jittered within the 1 um
    tolerance across cell boundaries, with gaps, branches and arcs."""
    rng = random.Random(11)
    for trial in range(300):
        n = rng.randint(3, 12)
        ends = _ring(n, rng.uniform(0.0005, 5.0), rng.choice([0.0, 0.0003, 0.0007]), rng)
        if trial % 5 == 1:
            ends.append(ends[rng.randrange(n)])  # a duplicate (branch)
        if trial % 7 == 2:
            a, b = ends[0]
            ends[0] = (a, (b[0] + 0.01, b[1]))  # a gap
        rng.shuffle(ends)
        shapes = [_seg(a, b) if rng.random() < 0.5 else _seg(b, a) for a, b in ends]
        if trial % 3 == 0:
            arc = Shape("arc", "F.CrtYd", ((0, 0), (1, 1), (2, 0)), None, None, 0.05, False)
            shapes.append(arc)
        pieces = [
            [s.points[0], s.points[1]] if s.kind == "segment" else geom._arc_points(*s.points)
            for s in shapes
        ]
        assert geom.closed_outline(shapes) == _closed_outline_linear(pieces), trial


def test_board_size() -> None:
    assert geom.board_size(_pad(size=(1.0, 2.0), rot=0.0)) == (1.0, 2.0)
    assert geom.board_size(_pad(size=(1.0, 2.0), rot=90.0)) == (2.0, 1.0)
    assert geom.board_size(_pad(size=(1.0, 2.0), rot=-90.0)) == (2.0, 1.0)
    assert geom.board_size(_pad(size=(1.0, 2.0), rot=180.0)) == (1.0, 2.0)


def test_cross_pairs_matches_brute_force() -> None:
    rnd = random.Random(5)

    def boxes(n: int, w: float) -> list[tuple[float, float, float, float]]:
        out = []
        for _ in range(n):
            x, y = rnd.uniform(0, 50), rnd.uniform(0, 50)
            out.append((x, y, x + rnd.uniform(0, w), y + rnd.uniform(0, w)))
        return out

    a, b = boxes(300, 3), boxes(40, 20)  # few long boxes (edges) against many small ones
    a.append(b[0])  # identical boxes, same x0
    gap = 0.25
    brute = {
        (i, j)
        for i in range(len(a))
        for j in range(len(b))
        if a[i][0] - gap <= b[j][2]
        and b[j][0] - gap <= a[i][2]
        and a[i][1] - gap <= b[j][3]
        and b[j][1] - gap <= a[i][3]
    }
    got = list(geom.cross_pairs(a, b, gap))
    assert len(got) == len(set(got))
    assert set(got) == brute


def test_sweeps_and_outline_stop_when_work_is_refused() -> None:
    """`afford` is charged before each box's scan; a refusal ends the iteration."""
    boxes = [(0.0, float(i), 1.0, i + 1.05) for i in range(50)]  # x: all; y: neighbours
    spent: list[int] = []

    def budget(limit: int) -> geom.Afford:
        def afford(n: int) -> bool:
            if sum(spent) + n > limit:
                return False
            spent.append(n)
            return True

        return afford

    full = list(geom.candidate_pairs(boxes, 0.1, budget(10**6)))
    assert full == list(geom.candidate_pairs(boxes, 0.1)) and len(full) == 49
    assert sum(spent) == sum(range(1, 51))  # 1 + the active boxes, per box
    spent.clear()
    assert 0 < len(list(geom.candidate_pairs(boxes, 0.1, budget(100)))) < 49
    spent.clear()
    assert list(geom.cross_pairs(boxes, boxes, 0.1, budget(10))) != list(
        geom.cross_pairs(boxes, boxes, 0.1)
    )
    segs = [Shape("segment", "F.CrtYd", ((0, 0), (0, 0)), None, None, 0.05, False)] * 10
    spent.clear()
    assert geom.closed_outline(segs, budget(5)) is None


# --- review-loop round 4: exact rounded pads, chord counts --------------------------------


@pytest.mark.parametrize(
    ("shape", "size", "ratio", "kind"),
    [
        ("roundrect", (1.475, 0.25), 0.25, geom.Rounded),
        ("roundrect", (1.0, 1.0), 0.0, list),
        ("oval", (1.6, 0.8), None, geom.Seg),
        ("oval", (0.8, 0.8), None, tuple),  # a circle
    ],
)
def test_distance_parts_of_rounded_pads(
    shape: str, size: tuple[float, float], ratio: float | None, kind: type
) -> None:
    """A roundrect / oval pad's distance part is small and exact: distances agree with
    its 68-vertex chord polygon to within the chord sagitta (inscribed: never larger)."""
    rng = random.Random(4)
    for rot in (0.0, 30.0, 90.0):
        pad = _pad(shape, at=(0.3, -0.2), size=size, rot=rot, ratio=ratio)
        (main,) = geom.pad_distance_parts(pad)
        assert isinstance(main, kind)
        chords = geom.pad_polygon(pad)
        for _ in range(50):
            other = ("circle", (rng.uniform(-3, 3), rng.uniform(-3, 3)), 0.05)
            exact, approx = geom.distance(main, other), geom.distance(chords, other)
            assert exact - 1e-9 <= approx <= exact + 0.002 * max(size)


def test_rounded_pad_gap_is_exact() -> None:
    """Two 0.3 mm roundrect pads at 0.4 mm pitch: exactly 0.1 mm apart, 4 core vertices."""
    a, b = (_pad("roundrect", at=(0, y), size=(1.475, 0.3), ratio=0.25) for y in (0, 0.4))
    (pa,), (pb,) = geom.pad_distance_parts(a), geom.pad_distance_parts(b)
    assert isinstance(pa, geom.Rounded) and len(pa.core) == 4
    assert geom.distance(pa, pb) == pytest.approx(0.1, abs=1e-12)


@pytest.mark.parametrize(
    "shape",
    [
        Shape("segment", "F.SilkS", ((0, 0), (1, 0)), None, None, 0.1, False),
        Shape("rect", "F.SilkS", ((0, 0), (1, 1)), None, None, 0.1, False),
        Shape("arc", "F.SilkS", ((0, 0), (0.5, 0.5), (1, 0)), None, None, 0.1, False),
        Shape("arc", "F.SilkS", ((0, 0), (0.01, 0.0001), (0.02, 0)), None, None, 0.1, False),
        Shape("arc", "F.SilkS", ((0, 0), (1, 1), (2, 2)), None, None, 0.1, False),  # line
        Shape("circle", "F.SilkS", (), (0, 0), (1, 1), 0.1, False),
        Shape("ellipse", "F.SilkS", (), (0, 0), (2, 1), 0.1, False),
        Shape("polygon", "F.SilkS", ((0, 0), (1, 0), (1, 1)), None, None, 0.1, True),
        Shape("polygon", "F.SilkS", ((0, 0), (1, 0)), None, None, 0.1, False),
        Shape("bezier", "F.SilkS", ((0, 0), (1, 0), (1, 1), (2, 1)), None, None, 0.1, False),
        Shape("ellipse_arc", "F.SilkS", (), (0, 0), (2, 1), 0.1, False),
    ],
)
def test_segment_count_matches_tessellation(shape: Shape) -> None:
    assert geom.segment_count(shape) == len(geom.stroke_segments(shape))


def test_closed_outline_charges_arc_chords_first() -> None:
    """The arcs' chords are charged before they are built: refused, nothing is built."""
    arcs = [
        Shape("arc", "F.CrtYd", ((0, 0), (1, 1), (2, 0)), None, None, 0.05, False),
        Shape("arc", "F.CrtYd", ((2, 0), (1, -1), (0, 0)), None, None, 0.05, False),
    ]
    asked: list[int] = []

    def refuse(cost: int) -> bool:
        asked.append(cost)
        return False

    assert geom.closed_outline(arcs, refuse) is None
    assert asked == [sum(geom.segment_count(s) + 1 for s in arcs)]
    assert geom.closed_outline(arcs, lambda cost: True) is not None


def test_band_index_matches_point_in_polygon() -> None:
    rng = random.Random(7)
    star = [
        (
            (1.0 if k % 2 else 0.4) * math.cos(k * math.pi / 9),
            (1.0 if k % 2 else 0.4) * math.sin(k * math.pi / 9),
        )
        for k in range(18)
    ]
    for poly in (star, [(0, 0), (2, 0), (2, 1), (1, 1), (1, 2), (0, 2)], [(0, 0), (1, 0), (2, 0)]):
        index = geom.BandIndex(poly)
        assert geom.BandIndex.build_cost(poly) >= sum(len(b) for b in index.bands)
        pts = [(rng.uniform(-1.2, 2.2), rng.uniform(-1.2, 2.2)) for _ in range(2000)]
        pts += list(poly) + [
            ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in itertools.pairwise(poly)
        ]
        for q in pts:
            assert index.contains(q) == geom.point_in_polygon(q, poly), (poly, q)


# --- verification pass ------------------------------------------------------------------


def test_is_convex_rejects_self_intersection() -> None:
    square = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
    assert geom.is_convex(square) and geom.is_convex(square[::-1])
    assert geom.is_convex([(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (2.0, 2.0), (2.0, 2.0), (0.0, 2.0)])
    star = [(math.sin(a), -math.cos(a)) for a in (2 * math.pi * k / 5 for k in (0, 2, 4, 1, 3))]
    assert not geom.is_convex(star)  # every turn the same way, but it winds twice
    assert not geom.is_convex([(0.0, 0.0), (2.0, 0.0), (1.0, 0.0), (1.0, 1.0)])  # back-track
    bow = [(0.0, 0.0), (2.0, 2.0), (2.0, 0.0), (0.0, 2.0)]
    assert not geom.is_convex(bow)


def test_segment_contacts() -> None:
    a, b = (0.0, 0.0), (4.0, 0.0)
    assert geom.segment_contacts(a, b, (2.0, -1.0), (2.0, 1.0)) == [0.5]  # crossing
    assert geom.segment_contacts(a, b, (1.0, 0.0), (1.0, 1.0)) == [0.25]  # touching
    assert geom.segment_contacts(a, b, (4.0, -1.0), (4.0, 1.0)) == [1.0]  # at an end
    assert geom.segment_contacts(a, b, (1.0, 0.0), (3.0, 0.0)) == [0.25, 0.75]  # overlap
    assert geom.segment_contacts(a, b, (3.0, 0.0), (9.0, 0.0)) == [0.75, 1.0]
    assert geom.segment_contacts(a, b, (4.0, 0.0), (9.0, 0.0)) == [1.0]  # end to end
    assert geom.segment_contacts(a, b, (5.0, 0.0), (9.0, 0.0)) == []  # collinear, apart
    assert geom.segment_contacts(a, b, (1.0, 0.5), (3.0, 0.5)) == []  # parallel
    assert geom.segment_contacts(a, b, (2.0, 0.1), (2.0, 1.0)) == []  # short of it
    assert geom.segment_contacts(a, a, (0.0, -1.0), (0.0, 1.0)) == []  # a point
