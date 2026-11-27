"""Pure plane / fanout / stitching candidates (SP4 P7 Task 1, `cc/board/planes.py`)."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from copperchat.board.classify import classify
from copperchat.board.geom import rect_poly
from copperchat.board.model import BoardModel, Pad, Part, Point
from copperchat.board.planes import (
    ZoneSpec,
    default_planes,
    fanout_candidates,
    fanout_pads,
    stitch_candidates,
)

from .builders import board, outline, part, track, zone

VIA = (0.6, 0.3)


def _pad(
    ref: str,
    number: str,
    net: str,
    at: Point,
    *,
    w: float = 1.0,
    h: float = 0.6,
    smd: bool = True,
    layers: frozenset[str] = frozenset({"F.Cu", "F.Paste", "F.Mask"}),
    drill: float | None = None,
) -> Pad:
    return Pad(ref, number, net, at[0], at[1], w, h, 0.0, "rect", layers, smd, drill)


def _part(ref: str, at: Point, pads: tuple[Pad, ...], half: float = 2.0) -> Part:
    return part(ref, at[0], at[1], half=half, pads=pads)


def _board(*parts: Part, layers: tuple[str, ...] = ("F.Cu", "B.Cu"), **kw: object) -> BoardModel:
    return board(
        parts=parts,
        copper_layers=layers,
        layer_types=tuple((n, "signal") for n in layers),
        rules=(
            ("min_clearance", 0.2),
            ("min_track_width", 0.2),
            ("copper_edge_clearance", 0.3),
            ("hole_to_hole_min", 0.25),
        ),
        **kw,
    )


def _gnd_board(n: int) -> BoardModel:
    layers = {2: ("F.Cu", "B.Cu"), 4: ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")}.get(
        n, ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu")
    )
    u1 = _part(
        "U1",
        (30, 30),
        (
            _pad("U1", "1", "GND", (29, 30)),
            _pad("U1", "2", "AGND", (31, 30)),
        ),
    )
    c1 = _part(
        "C1", (40, 30), (_pad("C1", "1", "GND", (39.5, 30)), _pad("C1", "2", "+3V3", (40.5, 30)))
    )
    return _board(u1, c1, layers=layers)


def _angle_ok(a: Point, b: Point) -> bool:
    ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 45.0
    return min(ang, 45.0 - ang) < 1e-9 or abs(abs(b[0] - a[0]) - abs(b[1] - a[1])) < 1e-9


def _outside_len(q: Pad, at: Point) -> float:
    """Stub length outside the (axis-aligned) pad rectangle along the stub."""
    dx, dy = at[0] - q.x, at[1] - q.y
    d = math.hypot(dx, dy)
    ux, uy = dx / d, dy / d
    tx = (q.w / 2) / abs(ux) if abs(ux) > 1e-12 else math.inf
    ty = (q.h / 2) / abs(uy) if abs(uy) > 1e-12 else math.inf
    return d - min(tx, ty)


# -- default planes ------------------------------------------------------------------------


def test_default_planes_4l() -> None:
    m = _gnd_board(4)
    assert default_planes(m, classify(m)) == (
        ZoneSpec("GND_In1", "GND", ("In1.Cu",), 10),
        ZoneSpec("GND_F", "GND", ("F.Cu",), 1),
        ZoneSpec("GND_B", "GND", ("B.Cu",), 1),
    )


def test_default_planes_2l() -> None:
    m = _gnd_board(2)
    assert default_planes(m, classify(m)) == (
        ZoneSpec("GND_B", "GND", ("B.Cu",), 10),
        ZoneSpec("GND_F", "GND", ("F.Cu",), 1),
    )


def test_default_planes_6l() -> None:
    m = _gnd_board(6)
    assert default_planes(m, classify(m)) == (
        ZoneSpec("GND_In1", "GND", ("In1.Cu",), 10),
        ZoneSpec("GND_In4", "GND", ("In4.Cu",), 10),
        ZoneSpec("GND_F", "GND", ("F.Cu",), 1),
        ZoneSpec("GND_B", "GND", ("B.Cu",), 1),
    )


def test_default_planes_most_pads_ground() -> None:
    """GND (2 pads) wins over AGND (1 pad)."""
    m = _gnd_board(4)
    assert {z.net for z in default_planes(m, classify(m))} == {"GND"}


def test_default_planes_without_ground() -> None:
    m = _board(_part("R1", (30, 30), (_pad("R1", "1", "N1", (29, 30)),)))
    assert default_planes(m, classify(m)) == ()


# -- fanout --------------------------------------------------------------------------------


def _free_pad_board() -> tuple[BoardModel, Pad]:
    q = _pad("U1", "1", "GND", (50, 40), w=1.0, h=0.6)
    return _board(_part("U1", (50, 40), (q,), half=1.0)), q


def test_fanout_16_candidates_free_pad() -> None:
    m, q = _free_pad_board()
    got = fanout_candidates(m, "GND", via=VIA, width=0.25, clearance=0.2)
    cands = got[("U1", "1")]
    assert len(cands) == 16
    assert [c.rank for c in cands] == list(range(16))
    for c in cands:
        assert c.stub is not None and c.pad == ("U1", "1")
        a, b = c.stub
        assert a == (q.x, q.y) and b == c.at
        assert _angle_ok(a, b), c
        assert _outside_len(q, c.at) <= 1.0 + 1e-9
    # d1 candidates come first
    dists = [math.dist((q.x, q.y), c.at) - 0.0 for c in cands]
    assert all(_outside_len(q, c.at) < 0.6 for c in cands[:8])
    assert all(_outside_len(q, c.at) > 0.9 for c in cands[8:])
    assert len(set(dists)) > 1


def test_fanout_diagonal_exact_on_nm_grid() -> None:
    m, q = _free_pad_board()
    cands = fanout_candidates(m, "GND", via=VIA, width=0.25, clearance=0.2)[("U1", "1")]
    for c in cands:
        dx, dy = c.at[0] - q.x, c.at[1] - q.y
        if abs(dx) > 1e-9 and abs(dy) > 1e-9:
            assert abs(abs(dx) - abs(dy)) < 1e-9
        for v in c.at:
            assert abs(v * 1e6 - round(v * 1e6)) < 1e-3


def test_fanout_prefers_away_from_footprint_centre() -> None:
    q = _pad("U1", "1", "GND", (50, 40), w=0.6, h=0.6)
    other = _pad("U1", "2", "N1", (48, 40), w=0.6, h=0.6)
    m = _board(part("U1", 49, 40, half=2.0, pads=(q, other)))
    cands = fanout_candidates(m, "GND", via=VIA, width=0.25, clearance=0.2)[("U1", "1")]
    first = cands[0]
    assert first.at[0] > q.x and abs(first.at[1] - q.y) < 1e-9  # +x points away from x=49


def test_fanout_no_via_in_pad() -> None:
    pitch = 0.65
    pads = tuple(
        _pad("U1", str(i + 1), "GND" if i % 2 == 0 else f"N{i}", (40 + i * pitch, 40), w=0.4, h=1.5)
        for i in range(8)
    )
    m = _board(part("U1", 42.3, 40, half=3.0, pads=pads))
    got = fanout_candidates(m, "GND", via=VIA, width=0.2, clearance=0.2)
    assert set(got) == {("U1", str(i + 1)) for i in range(0, 8, 2)}
    for cands in got.values():
        for c in cands:
            for q in pads:
                lx, ly = abs(c.at[0] - q.x), abs(c.at[1] - q.y)
                dx, dy = max(lx - q.w / 2, 0.0), max(ly - q.h / 2, 0.0)
                assert math.hypot(dx, dy) >= VIA[0] / 2 - 1e-9, (c, q)


def test_fanout_other_net_clearance() -> None:
    m, _q = _free_pad_board()
    blocker = track("SIG", "F.Cu", (51.2, 38), (51.2, 42), 0.2, kiid="t1")
    m = replace(m, tracks=(blocker,))
    cands = fanout_candidates(m, "GND", via=VIA, width=0.25, clearance=0.2)[("U1", "1")]
    for c in cands:
        assert c.at[0] < 51.2 - 0.1 - 0.2 - 0.3 + 1e-9 or c.at[0] > 51.2 + 0.6


def test_fanout_max_stub() -> None:
    m, q = _free_pad_board()
    # d1 outside the pad = clearance 0.1 + via radius 0.15 = 0.25; d2 = 0.75
    cands = fanout_candidates(m, "GND", via=(0.3, 0.15), width=0.2, clearance=0.1, max_stub=0.3)
    got = cands[("U1", "1")]
    assert len(got) == 8
    assert all(_outside_len(q, c.at) <= 0.3 + 1e-9 for c in got)


def test_fanout_skips_tht() -> None:
    tht = _pad(
        "J1",
        "1",
        "GND",
        (30, 30),
        smd=False,
        layers=frozenset({"*.Cu", "*.Mask"}),
        drill=1.0,
        w=1.7,
        h=1.7,
    )
    smd = _pad("U1", "1", "GND", (50, 40))
    m = _board(_part("J1", (30, 30), (tht,)), _part("U1", (50, 40), (smd,)))
    got = fanout_candidates(m, "GND", via=VIA, width=0.25, clearance=0.2)
    assert set(got) == {("U1", "1")}


def test_fanout_skips_connected_pad() -> None:
    m, q = _free_pad_board()
    m = replace(m, tracks=(track("GND", "F.Cu", (50, 40), (52, 40), kiid="t"),))
    todo, done = fanout_pads(m, "GND")
    assert todo == [] and done == [q]
    assert fanout_candidates(m, "GND", via=VIA, width=0.25, clearance=0.2) == {}


def test_fanout_outline_inset() -> None:
    q = _pad("U1", "1", "GND", (1.2, 40))
    m = _board(_part("U1", (1.2, 40), (q,), half=0.8))
    cands = fanout_candidates(m, "GND", via=VIA, width=0.25, clearance=0.2)[("U1", "1")]
    assert cands and all(c.at[0] >= 0.3 + 0.3 - 1e-9 for c in cands)


def test_fanout_via_keepout() -> None:
    m, _q = _free_pad_board()
    ko = zone(
        "KO",
        "",
        ("F.Cu", "B.Cu"),
        tuple(rect_poly(50.6, 35, 5, 10)),
        rule_area=True,
        keepout=frozenset({"vias"}),
    )
    m = replace(m, zones=(ko,))
    cands = fanout_candidates(m, "GND", via=VIA, width=0.25, clearance=0.2)[("U1", "1")]
    assert cands and all(c.at[0] + 0.3 <= 50.6 + 1e-9 for c in cands)


def test_fanout_no_candidate_maps_to_empty() -> None:
    q = _pad("U1", "1", "GND", (50, 40), w=0.3, h=0.3)
    ring = tuple(
        _pad("U1", str(i + 2), f"N{i}", (50 + 0.6 * dx, 40 + 0.6 * dy), w=0.3, h=0.3)
        for i, (dx, dy) in enumerate(
            [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
        )
    )
    m = _board(part("U1", 50, 40, half=1.5, pads=(q, *ring)))
    assert fanout_candidates(m, "GND", via=VIA, width=0.2, clearance=0.2) == {("U1", "1"): ()}


# -- stitching -----------------------------------------------------------------------------


def _stitch_board(layers: tuple[str, ...] = ("F.Cu", "B.Cu"), **kw: object) -> BoardModel:
    full = tuple(rect_poly(0, 0, 50, 40))
    zones = tuple(zone(f"GND_{n}", "GND", (n,), full, kiid=f"z{i}") for i, n in enumerate(layers))
    parts = kw.pop("parts", ())
    return _board(*parts, zones=zones, outline=outline(0, 0, 50, 40), **kw)  # type: ignore[arg-type]


def test_stitch_grid_count() -> None:
    m = _stitch_board(parts=())
    got = stitch_candidates(m, "GND", via=VIA, clearance=0.2, pitch=5.0)
    assert len(got) == 10 * 8
    assert got[0].at == (2.5, 2.5) and got[-1].at == (47.5, 37.5)
    assert all(c.stub is None and c.pad is None for c in got)


def test_stitch_grid_filters_parts_and_tracks() -> None:
    u1 = _part("U1", (12.5, 12.5), (_pad("U1", "1", "N1", (12.5, 12.5)),), half=1.5)
    m = _stitch_board(parts=(u1,))
    m = replace(m, tracks=(track("SIG", "F.Cu", (0, 22.6), (50, 22.6), 0.2, kiid="t"),))
    got = {c.at for c in stitch_candidates(m, "GND", via=VIA, clearance=0.2, pitch=5.0)}
    assert (12.5, 12.5) not in got  # courtyard
    assert not any(abs(y - 22.5) < 1e-9 for _, y in got)  # 0.1 mm from the track
    assert (7.5, 7.5) in got


def test_stitch_edge_ring() -> None:
    m = _stitch_board(parts=())
    ring = stitch_candidates(m, "GND", via=VIA, clearance=0.2, pitch=5.0, edge_ring=True)
    plain = stitch_candidates(m, "GND", via=VIA, clearance=0.2, pitch=5.0)
    assert len(ring) > len(plain)
    inset = 0.3 + 0.3 + 0.5
    on_ring = [c.at for c in ring if c.at not in {p.at for p in plain}]
    assert on_ring
    for x, y in on_ring:
        d = min(abs(x - inset), abs(50 - inset - x), abs(y - inset), abs(40 - inset - y))
        assert d < 1e-6, (x, y)
    ats = [c.at for c in ring]
    for i, a in enumerate(ats):
        for b in ats[i + 1 :]:
            assert math.dist(a, b) >= 0.8 - 1e-9


def test_stitch_needs_two_zone_layers() -> None:
    m = _stitch_board(layers=("F.Cu",), parts=())
    assert stitch_candidates(m, "GND", via=VIA, clearance=0.2) == ()


def test_stitch_no_outline() -> None:
    m = _stitch_board(parts=())
    m = replace(m, outline=replace(m.outline, kind="none"))
    assert stitch_candidates(m, "GND", via=VIA, clearance=0.2) == ()


def test_deterministic() -> None:
    m = _gnd_board(4)
    a = fanout_candidates(m, "GND", via=VIA, width=0.25, clearance=0.2)
    b = fanout_candidates(m, "GND", via=VIA, width=0.25, clearance=0.2)
    assert a == b and list(a) == [("C1", "1"), ("U1", "1")]  # natural ref order
    s = _stitch_board(parts=())
    assert stitch_candidates(s, "GND", via=VIA, clearance=0.2, edge_ring=True) == (
        stitch_candidates(s, "GND", via=VIA, clearance=0.2, edge_ring=True)
    )


def test_fanout_repeated_pad_numbers_keyed_apart() -> None:
    a = _pad("J1", "SH", "GND", (40, 40))
    b = _pad("J1", "SH", "GND", (50, 40))
    m = _board(part("J1", 45, 40, half=6.0, pads=(a, b)))
    got = fanout_candidates(m, "GND", via=VIA, width=0.25, clearance=0.2)
    assert list(got) == [("J1", "SH"), ("J1", "SH#2")]
    assert all(c.pad == ("J1", "SH#2") for c in got[("J1", "SH#2")])


def test_fanout_ties_a_boxed_in_pin_to_its_exposed_pad() -> None:
    """R7 (ODH TPS63000): a fine-pitch ground pin with no via spot (other-net pins 0.5 mm
    to both sides, other-net copper in front) gets, after every via candidate, a tie stub
    straight into the larger same-net pad of its own part (the exposed pad); never through
    other-net copper, never across a gap > TIE_MAX_GAP_MM."""
    from copperchat.board.planes import TIE_MAX_GAP_MM

    gnd = _pad("U1", "3", "GND", (50.0, 41.4), w=0.24, h=0.6)
    epad = _pad("U1", "11", "GND", (50.0, 40.0), w=2.4, h=1.65)
    left = _pad("U1", "2", "SW1", (49.5, 41.4), w=0.24, h=0.6)
    right = _pad("U1", "4", "SW2", (50.5, 41.4), w=0.24, h=0.6)
    block = _part("R9", (50.0, 42.6), (_pad("R9", "1", "X", (50.0, 42.6), w=2.0, h=0.6),))
    m = _board(part("U1", 50.0, 40.5, half=1.5, pads=(gnd, epad, left, right)), block)
    cands = fanout_candidates(m, "GND", via=VIA, width=0.2, clearance=0.2)[("U1", "3")]
    assert cands and all(not c.tie for c in cands[:-1]) and cands[-1].tie
    tie = cands[-1]
    assert tie.stub == ((50.0, 41.4), tie.at)
    # straight up into the exposed pad (its edge at y 40.825), the stub's half-width inside
    assert tie.at == pytest.approx((50.0, 40.825 - 0.1))
    # the exposed pad farther than TIE_MAX_GAP_MM: no tie
    far = _pad("U1", "11", "GND", (50.0, 41.4 - 0.3 - TIE_MAX_GAP_MM - 0.825 - 0.05), w=2.4, h=1.65)
    m3 = _board(part("U1", 50.0, 40.5, half=3.0, pads=(gnd, far, left, right)), block)
    assert not any(
        c.tie for c in fanout_candidates(m3, "GND", via=VIA, width=0.2, clearance=0.2)[("U1", "3")]
    )
    # the pin beside the exposed pad's span: the straight stub would miss it - no tie
    side = _pad("U1", "3", "GND", (51.4, 41.4), w=0.24, h=0.6)
    m4 = _board(part("U1", 50.0, 40.5, half=1.5, pads=(side, epad)), block)
    assert not any(
        c.tie for c in fanout_candidates(m4, "GND", via=VIA, width=0.2, clearance=0.2)[("U1", "3")]
    )
    # other-net copper across the gap: no tie
    wire = _part("R8", (50.0, 41.0), (_pad("R8", "1", "Y", (50.0, 41.0), w=0.6, h=0.1),))
    m2 = _board(part("U1", 50.0, 40.5, half=1.5, pads=(gnd, epad, left, right)), block, wire)
    assert not any(
        c.tie for c in fanout_candidates(m2, "GND", via=VIA, width=0.2, clearance=0.2)[("U1", "3")]
    )
