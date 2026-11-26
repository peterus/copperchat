"""Round 2 (R1, B-C5): entry / escape zones, neck width, straight corridors, entry loops."""

from __future__ import annotations

import math

import pytest

from copperchat.board.geom import (
    ADJ_GAP_MM,
    ESCAPE_HALO_MM,
    LOOP_TURN_DEG,
    Point,
    _in_pad,
    analyse_entry,
    entry_zone,
    entry_zone_width,
    escape_zone,
    neck_width,
    point_in_polygon,
    straight_corridor,
)
from copperchat.board.model import Pad
from tests.unit.board.builders import part


def pad(
    x: float = 0.0,
    y: float = 0.0,
    w: float = 1.0,
    h: float = 1.3,
    *,
    rot: float = 0.0,
    layers: frozenset[str] = frozenset({"F.Cu", "F.Mask", "F.Paste"}),
    smd: bool = True,
    drill: float | None = None,
    ref: str = "R1",
) -> Pad:
    return Pad(ref, "1", "N1", x, y, w, h, rot, "rect", layers, smd, drill)


def test_constants() -> None:
    assert (ADJ_GAP_MM, ESCAPE_HALO_MM, LOOP_TURN_DEG) == (3.0, 0.5, 135.0)
    assert entry_zone_width(0.2) == 1.0
    assert entry_zone_width(0.5) == pytest.approx(1.5)


def test_neck_width() -> None:
    assert neck_width(pad(w=0.25, h=0.8), 0.5, 0.127) == 0.25
    assert neck_width(pad(w=0.25, h=0.8), 0.2, 0.127) == 0.2
    assert neck_width(pad(w=0.1, h=0.5), 0.5, 0.127) == 0.127


def test_escape_zone_is_courtyard_plus_halo() -> None:
    z = escape_zone(part("U1", 10, 20, half=2.0))
    xs, ys = [p[0] for p in z], [p[1] for p in z]
    assert max(xs) - min(xs) == pytest.approx(5.0)
    assert max(ys) - min(ys) == pytest.approx(5.0)
    assert (min(xs), min(ys)) == pytest.approx((7.5, 17.5))


def test_escape_zone_without_courtyard_uses_pads() -> None:
    p = part("U1", 10, 20, courtyard=(), pads=(pad(10, 20, 1.0, 2.0),))
    z = escape_zone(p)
    xs, ys = [q[0] for q in z], [q[1] for q in z]
    assert (min(xs), max(xs)) == pytest.approx((9.0, 11.0))
    assert (min(ys), max(ys)) == pytest.approx((18.5, 21.5))
    assert escape_zone(part("U2", courtyard=())) == ()


def test_entry_zone_rotated() -> None:
    z = entry_zone(pad(5, 5, 1.0, 0.4, rot=90.0), 0.2)
    xs, ys = [p[0] for p in z], [p[1] for p in z]
    # rotated by 90°: 0.4 wide in x, 1.0 in y, grown by 1.0 each side
    assert max(xs) - min(xs) == pytest.approx(2.4)
    assert max(ys) - min(ys) == pytest.approx(3.0)


def test_pad_rect_matches_in_pad() -> None:
    q = pad(3, 4, 2.0, 0.6, rot=30.0)
    z = entry_zone(q, 0.1)  # grown by 1.0 mm
    for p in z:
        assert not _in_pad(p, q)
    centre_shift = [(3 + 0.9 * math.cos(math.radians(-30)), 4 + 0.9 * math.sin(math.radians(-30)))]
    for p in centre_shift:  # 0.9 mm along the pad's long local axis: inside the pad
        assert _in_pad(p, q)
        assert point_in_polygon(p, z)


def test_corridor_aligned_pads() -> None:
    a, b = pad(0, 0), pad(3.0, 0)  # 1.0 wide: edges at 0.5 and 2.5, gap 2.0
    got = straight_corridor(a, b, 0.25)
    assert got is not None
    layers, s, e = got
    assert layers == ("F.Cu",)
    assert s == pytest.approx((0.5, 0.0)) and e == pytest.approx((2.5, 0.0))
    # reversed order: start on the first pad's edge
    _, s2, e2 = straight_corridor(b, a, 0.25) or ((), (0, 0), (0, 0))
    assert s2 == pytest.approx((2.5, 0.0)) and e2 == pytest.approx((0.5, 0.0))
    # offset 0.5 in y: overlap band [-0.15, 0.65] -> centre 0.25
    got = straight_corridor(a, pad(3.0, 0.5), 0.25)
    assert got is not None
    assert got[1] == pytest.approx((0.5, 0.25)) and got[2] == pytest.approx((2.5, 0.25))
    assert straight_corridor(a, pad(3.0, 1.2), 0.25) is None  # overlap 0.1 < 0.25
    assert straight_corridor(a, pad(4.5, 0), 0.25) is None  # gap 3.5
    # vertical pair
    got = straight_corridor(pad(0, 0, 1.3, 1.0), pad(0, 2.5, 1.3, 1.0), 0.25)
    assert got is not None
    assert got[1] == pytest.approx((0.0, 0.5)) and got[2] == pytest.approx((0.0, 2.0))


def test_corridor_overlapping_pads_none() -> None:
    assert straight_corridor(pad(0, 0), pad(0.5, 0), 0.25) is None


def test_corridor_needs_shared_layer() -> None:
    a = pad(0, 0)
    b = pad(3.0, 0, layers=frozenset({"B.Cu", "B.Mask"}))
    assert straight_corridor(a, b, 0.25) is None


def test_corridor_tht_pairs() -> None:
    tht = frozenset({"*.Cu", "*.Mask"})
    a = pad(0, 0, layers=tht, smd=False, drill=0.8)
    b = pad(3.0, 0, layers=tht, smd=False, drill=0.8)
    four = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
    got = straight_corridor(a, b, 0.25, copper_layers=four)
    assert got is not None and got[0] == four
    smd_bottom = pad(3.0, 0, layers=frozenset({"B.Cu"}))
    got = straight_corridor(a, smd_bottom, 0.25, copper_layers=four)
    assert got is not None and got[0] == ("B.Cu",)


def walk(start: Point, heading: float, steps: list[tuple[float, float]]) -> list[Point]:
    pts = [start]
    h = heading
    for turn, length in steps:
        h += turn
        x, y = pts[-1]
        pts.append((x + length * math.cos(math.radians(h)), y + length * math.sin(math.radians(h))))
    return pts


P = pad(0, 0, 1.0, 1.0)
ZONE = entry_zone(P, 0.2)  # pad grown by 1.0: [-1.5, 1.5]^2


def test_entry_loop() -> None:
    pts = walk((0, 0), 0, [(0, 0.8), (45, 0.4), (45, 0.4), (45, 0.4), (0, 3.0)])
    r = analyse_entry(pts, 0.2, ZONE)
    assert r.loops == (pytest.approx(pts[3]),)
    # the same turns outside the zone: no loop
    far = walk((0, 0), 0, [(0, 3.0), (45, 0.4), (45, 0.4), (45, 0.4), (0, 3.0)])
    assert analyse_entry(far, 0.2, ZONE).loops == ()


def test_entry_staircase_not_loop() -> None:
    pts = walk((0, 0), 0, [(0, 0.6), (45, 0.3), (-45, 0.3), (45, 0.3), (0, 3.0)])
    r = analyse_entry(pts, 0.2, ZONE)
    assert r.loops == ()
    assert len(r.jogs) == 2


def test_entry_jog() -> None:
    pts = walk((0, 0), 0, [(0, 0.6), (45, 0.4), (-45, 3.0)])
    r = analyse_entry(pts, 0.2, ZONE)
    assert r.jogs == (pytest.approx(pts[2]),)
    assert r.loops == ()


def test_entry_plain_45_ok() -> None:
    pts = walk((0, 0), 0, [(0, 0.8), (45, 3.0)])
    assert analyse_entry(pts, 0.2, ZONE) == analyse_entry([], 0.2, ZONE)
    r = analyse_entry(pts, 0.2, ZONE)
    assert r.loops == () and r.jogs == ()


def test_entry_bends_inside_pad_ignored() -> None:
    # first bend at 0.3 mm (inside the 1 x 1 pad): only one counted bend remains
    pts = walk((0, 0), 0, [(0, 0.3), (45, 0.5), (-45, 3.0)])
    assert analyse_entry(pts, 0.2, ZONE).jogs != ()
    r = analyse_entry(pts, 0.2, ZONE, in_pad=lambda p: _in_pad(p, P))
    assert r.jogs == () and r.loops == ()


def test_entry_acute_is_loop() -> None:
    pts = walk((0, 0), 0, [(0, 0.8), (150, 3.0)])
    assert analyse_entry(pts, 0.2, ZONE).loops == (pytest.approx(pts[1]),)
