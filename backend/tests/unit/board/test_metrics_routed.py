"""B-C7 metrics, routed side: connections, routed_length, M4-M7, M8 routed, M11-M13."""

from __future__ import annotations

import math
import time
from dataclasses import replace

import pytest

from copperchat.board.boardmd import BoardDoc
from copperchat.board.classify import Classification, Decoupling, DiffPair, classify
from copperchat.board.geom import rect_poly
from copperchat.board.metrics import (
    Metric,
    MetricsReport,
    compute_metrics,
    connections,
    routed_length,
)
from copperchat.board.model import ArcTrack, BoardModel, Part, Point, Zone
from copperchat.board.presets import PRESETS

from .builders import board, net_class, part, via, zone
from .builders_circuit import cap, circuit, conn, ic, pad, route, routed_board, with_layers


def run(m: BoardModel, cls: Classification | None = None, **kw: object) -> MetricsReport:
    return compute_metrics(
        m,
        cls if cls is not None else classify(m),
        BoardDoc(),
        drc=None,
        parity=None,
        preset=None,
        **kw,  # type: ignore[arg-type]
    )


def get(rep: MetricsReport, mid: str) -> Metric:
    return next(x for x in rep.metrics if x.id == mid)


def ends(ref: str, net: str, a: Point, b: Point) -> Part:
    """A part with one pad at `a` and one at `b`, both on `net`."""
    return part(ref, a[0], a[1], pads=(pad(ref, "1", net, a), pad(ref, "2", net, b)))


# ---- connections / routed_length -----------------------------------------------------------


def test_connections_mst() -> None:
    j1 = conn("J1", {"1": "N1", "2": "N1", "3": "N1", "4": "N1"}, (10, 40))
    m = circuit(j1, cap("C1", "N2", "N3"))
    pairs = connections(m, "N1")
    assert len(pairs) == 3
    for a, b in pairs:
        assert math.dist((a.x, a.y), (b.x, b.y)) == pytest.approx(1.0)
    assert {p.number for pair in pairs for p in pair} == {"1", "2", "3", "4"}
    assert connections(m, "N2") == []


def test_routed_length_via_zero() -> None:
    p = ends("R1", "N1", (10, 10), (30, 10))
    tracks = (
        *route("N1", [(10, 10), (20, 10)]),
        *route("N1", [(20, 10), (25, 10)], layer="B.Cu"),
        *route("N1", [(25, 10), (30, 10)]),
    )
    vias = (via("N1", (20, 10), kiid="v1"), via("N1", (25, 10), kiid="v2"))
    m = board(parts=(p,), tracks=tracks, vias=vias)
    a, b = p.pads
    assert routed_length(m, a, b) == pytest.approx(20.0)
    # without the second via the B.Cu piece does not reach the F.Cu piece
    assert routed_length(replace(m, vias=vias[:1]), a, b) is None


def test_routed_length_t_junction_and_pad_join() -> None:
    p = ends("R1", "N1", (10, 10), (20, 15))
    # the branch starts in the middle of the trunk; its end lies inside pad 2's copper
    tracks = (
        *route("N1", [(10, 10), (20, 10)]),
        *route("N1", [(15, 10), (15, 15), (19.8, 15)]),
    )
    m = board(parts=(p,), tracks=tracks)
    a, b = p.pads
    assert routed_length(m, a, b) == pytest.approx(5 + 5 + 4.8)


def test_routed_length_arc_and_via_on_track() -> None:
    p = ends("R1", "N1", (10, 10), (15, 15))
    c = math.sqrt(0.5)
    arc = ArcTrack("a1", "N1", "F.Cu", (10, 10), (15 - 5 * c, 10 + 5 * c), (15, 15), 0.2, False)
    m = board(parts=(p,), arcs=(arc,))
    assert routed_length(m, *p.pads) == pytest.approx(math.pi * 5 / 2)
    # a via in the middle of a B.Cu track joins it
    q = ends("R2", "N2", (10, 30), (30, 30))
    tracks = (
        *route("N2", [(10, 30), (20, 30)]),
        *route("N2", [(15, 30), (25, 30)], layer="B.Cu"),
        *route("N2", [(25, 30), (30, 30)]),
    )
    vias = (via("N2", (15, 30), kiid="v1"), via("N2", (25, 30), kiid="v2"))
    m = board(parts=(q,), tracks=tracks, vias=vias)
    assert routed_length(m, *q.pads) == pytest.approx(20.0)


# ---- M4 ------------------------------------------------------------------------------------


def _m4_a() -> tuple[Part, tuple[object, ...]]:
    return ends("RA", "NA", (10, 10), (20, 20)), route("NA", [(10, 10), (20, 10), (20, 20)])


def _m4_b() -> tuple[Part, tuple[object, ...]]:
    end = (20.0, 40.0 + 10 * math.tan(math.radians(0.6)))
    return ends("RB", "NB", (10, 40), end), route("NB", [(10, 40), end])


def _m4_c() -> tuple[Part, tuple[object, ...]]:
    pts = [(10.0, 60.0), (15.0, 60.0), (20.0, 65.0), (25.0, 65.0)]
    return ends("RC", "NC", pts[0], pts[-1]), route("NC", pts)


def _m4_board(*cases: tuple[Part, tuple[object, ...]]) -> BoardModel:
    return board(
        parts=tuple(c[0] for c in cases),
        tracks=tuple(t for c in cases for t in c[1]),  # type: ignore[misc]
    )


def test_m4_counts() -> None:
    assert get(run(_m4_board(_m4_a())), "M4").value == 1
    assert get(run(_m4_board(_m4_b())), "M4").value == 1
    clean = get(run(_m4_board(_m4_c())), "M4")
    assert clean.value == 0
    assert clean.verdict == "pass"
    both = get(run(_m4_board(_m4_a(), _m4_b(), _m4_c())), "M4")
    assert both.value == 2
    assert both.verdict == "fail"


def test_m4_off_angle_counts() -> None:
    # one 60 degree vertex: the vertex is an off-angle bend; its 60 degree leg is itself a
    # non-octilinear segment, so the plan's sum (non-octilinear + ... + off_angle) is 2
    leg = (20 + 5 * math.cos(math.radians(60)), 10 + 5 * math.sin(math.radians(60)))
    m = board(
        parts=(ends("RA", "NA", (10, 10), leg),), tracks=route("NA", [(10, 10), (20, 10), leg])
    )
    m4 = get(run(m), "M4")
    assert m4.verdict == "fail"
    whats = sorted(o.what for o in m4.offenders)
    assert [w for w in whats if "off_angle" in w] == ["off_angle"]
    assert m4.value == 2


# ---- M5 / M6 -------------------------------------------------------------------------------


def _detour(net: str, x0: float, y0: float, d: float, h: float) -> tuple[Part, tuple[object, ...]]:
    """Pads `d` mm apart on a horizontal line, routed around a detour of height `h`
    (routed length d + 2h)."""
    pts = [(x0, y0), (x0, y0 + h), (x0 + d, y0 + h), (x0 + d, y0)]
    return ends(f"R{net}", net, pts[0], pts[-1]), route(net, pts)


def test_m5_fail_needs_both() -> None:
    d = 4 / 1.5  # ratio 2.5, extra 4 mm
    not_fail = get(run(_m4_board(_detour("S1", 10, 10, d, 2.0))), "M5")
    assert not_fail.verdict == "review"
    assert not_fail.offenders[0].value == pytest.approx(2.5)
    d = 6 / 1.1  # ratio 2.1, extra 6 mm
    fail = get(run(_m4_board(_detour("S1", 10, 10, d, 3.0))), "M5")
    assert fail.verdict == "fail"
    assert fail.offenders[0].value == pytest.approx(2.1)


def test_m5_median_review() -> None:
    cases = [_detour(f"S{i}", 10, 10 + 5 * i, 10.0, 1.0) for i in range(3)]  # ratio 1.2 each
    m5 = get(run(_m4_board(*cases)), "M5")
    assert m5.verdict == "review"
    assert m5.value == pytest.approx(1.2)
    straight = [_detour(f"S{i}", 10, 10 + 5 * i, 10.0, 0.0) for i in range(3)]
    assert get(run(_m4_board(*straight)), "M5").verdict == "pass"


def test_m5_ignores_supply_and_short() -> None:
    m = _m4_board(_detour("+3V3", 10, 10, 2.0, 5.0), _detour("S1", 10, 30, 0.4, 3.0))
    m5 = get(run(m), "M5")
    assert m5.verdict in {"pass", "n/a"}


def test_m6_long() -> None:
    # board 100 x 80: diagonal 128.06 mm, half 64.03 mm
    long = _detour("S1", 10, 10, 70.0, 0.0)
    m6 = get(run(_m4_board(long)), "M6")
    assert m6.verdict == "review"
    assert m6.value == 1
    assert m6.hard is None
    short = _detour("S1", 10, 10, 60.0, 0.0)
    assert get(run(_m4_board(short)), "M6").verdict == "pass"


# ---- M7 ------------------------------------------------------------------------------------


def test_m7_counts_and_bends_per_10mm() -> None:
    # bends at B C D E F G; the pad-entry bends B and G are exempt; D-E is a micro-jog
    jog = [(10.0, 10.0), (12.0, 10.0), (13.0, 11.0), (15.0, 11.0), (15.2, 11.2), (17.0, 11.2)]
    jog += [(18.0, 12.2), (20.0, 12.2)]
    m = board(parts=(ends("RJ", "SJ", jog[0], jog[-1]),), tracks=route("SJ", jog))
    m7 = get(run(m), "M7")
    assert m7.value == 1
    assert m7.verdict == "fail"
    assert [o.what for o in m7.offenders if o.value is None] == ["micro_jog"]
    zig = [
        (10.0 + x, 30.0 + y)
        for x, y in [(0, 0), (2, 0), (3, 1), (5, 1), (6, 2), (8, 2), (9, 3), (11, 3)]
    ]
    m = board(parts=(ends("RZ", "SZ", zig[0], zig[-1]),), tracks=route("SZ", zig))
    m7 = get(run(m), "M7")
    assert m7.value == 0
    assert m7.verdict == "review"  # 4 bends over 12.2 mm = 3.3 bends / 10 mm
    straight = [(10.0, 30.0), (30.0, 30.0)]
    m = board(parts=(ends("RS", "SS", straight[0], straight[-1]),), tracks=route("SS", straight))
    assert get(run(m), "M7").verdict == "pass"


# ---- M8 routed -----------------------------------------------------------------------------


def test_m8_routed_5_1_fails() -> None:
    u1 = ic("U1", {"1": "+3V3", "2": "GND", "3": "A", "4": "B", "5": "C"}, (50, 40))
    c1 = cap("C1", "+3V3", "GND", (50.5, 45.1))  # +3V3 pad at (50, 45.1)
    m = circuit(u1, c1, tracks=route("+3V3", [(50, 40), (50, 45.1)]))
    cls = replace(classify(m), decoupling=(Decoupling("C1", "U1", "1", "+3V3", "GND"),))
    rep = run(m, cls)
    assert rep.stage == "routed"
    m8 = get(rep, "M8")
    assert m8.verdict == "fail"
    assert m8.value == pytest.approx(5.1)
    ok = circuit(
        u1, cap("C1", "+3V3", "GND", (50.5, 44.9)), tracks=route("+3V3", [(50, 40), (50, 44.9)])
    )
    assert get(run(ok, cls), "M8").verdict == "review"


# ---- M11 -----------------------------------------------------------------------------------

_FOUR = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")


def _plane(
    filled: tuple[tuple[Point, ...], ...],
    holes: tuple[tuple[tuple[Point, ...], ...], ...] = (),
    area: float = 0.0,
) -> Zone:
    z = zone("GNDP", "GND", ("In1.Cu",), tuple(rect_poly(0, 0, 100, 80)), filled_area_mm2=area)
    return replace(z, filled=filled, filled_holes=holes or tuple(() for _ in filled))


def _m11_board(z: Zone, tracks: tuple[object, ...] = (), **kw: object) -> BoardModel:
    return board(
        copper_layers=_FOUR,
        zones=(z,),
        tracks=tracks,
        parts=(part("R1", 30, 30, pads=(pad("R1", "1", "GND", (30, 30)),)),),
        **kw,
    )


def test_m11_fill_and_crossing() -> None:
    antipad = tuple(rect_poly(49, 29, 2, 2))
    fill = _plane((tuple(rect_poly(0, 0, 100, 64)),), ((antipad,),))  # 6400 - 4 of 8000
    vias = (via("S2", (50, 30), 1.0, kiid="v-s2"),)  # antipad corners within reach
    crossing = route("S1", [(20, 60), (20, 70)])  # leaves the fill at y = 64
    m11 = get(run(_m11_board(fill, crossing, vias=vias)), "M11")
    assert m11.verdict == "review"
    assert m11.value == pytest.approx((6400 - 4) / 8000, abs=1e-3)
    cross = [o for o in m11.offenders if "void" in o.what]
    assert len(cross) == 1
    assert cross[0].nets == ("S1",)
    # the same track over solid fill and a track through the via's antipad: no crossing
    full = _plane((tuple(rect_poly(0, 0, 100, 80)),), ((antipad,),))
    solid = (*route("S1", [(20, 10), (20, 20)]), *route("S2", [(45, 30), (50, 30)]))
    m11 = get(run(_m11_board(full, solid, vias=vias)), "M11")
    assert not [o for o in m11.offenders if "void" in o.what]
    assert m11.verdict == "pass"
    # an empty hole (no via, no pad inside) is a void
    void = tuple(rect_poly(40, 40, 10, 10))
    holed = _plane((tuple(rect_poly(0, 0, 100, 80)),), ((void,),))
    m11 = get(run(_m11_board(holed, route("S1", [(30, 45), (60, 45)]))), "M11")
    assert len([o for o in m11.offenders if "void" in o.what]) == 1
    # a track on a non-adjacent layer (B.Cu is next to In2.Cu, not In1.Cu) is ignored
    m11 = get(run(_m11_board(holed, route("S1", [(30, 45), (60, 45)], layer="B.Cu"))), "M11")
    assert not [o for o in m11.offenders if "void" in o.what]
    # ground tracks never count
    m11 = get(run(_m11_board(holed, route("GND", [(30, 45), (60, 45)]))), "M11")
    assert not [o for o in m11.offenders if "void" in o.what]


def test_m11_fallback_area_and_na() -> None:
    z = _plane((), area=7200.0)
    m11 = get(run(_m11_board(z)), "M11")
    assert m11.value == pytest.approx(0.9)
    assert m11.verdict == "pass"
    assert get(run(board()), "M11").verdict == "n/a"


def test_m11_unfilled_zone_review() -> None:
    m11 = get(run(_m11_board(_plane(()))), "M11")
    assert m11.verdict == "review"
    assert any("not filled" in o.what for o in m11.offenders)


# ---- M12 / M13 -----------------------------------------------------------------------------


def test_m12_width_and_mismatch() -> None:
    p_net = _detour("D_P", 10, 10, 10.0, 0.0)
    n_net = _detour("D_N", 10, 20, 10.0, 0.75)  # 11.5 mm: mismatch 1.5 mm
    m = replace(_m4_board(p_net, n_net), net_classes=(net_class("Default", 0.2),))
    cls = replace(classify(m), pairs=(DiffPair("D_P", "D_N", "U1"),))
    m12 = get(run(m, cls), "M12")
    assert m12.verdict == "review"
    assert m12.offenders[0].value == pytest.approx(1.5)
    thin = replace(m, tracks=(*m.tracks, *route("S9", [(10, 60), (20, 60)], width=0.15)))
    m12 = get(run(thin, cls), "M12")
    assert m12.verdict == "fail"
    assert m12.value == 1
    assert any(o.nets == ("S9",) and "width" in o.what for o in m12.offenders)
    exact = replace(m, tracks=(*m.tracks, *route("S9", [(10, 60), (20, 60)], width=0.1995)))
    assert get(run(exact, cls), "M12").value == 0


def test_m13_track_in_module_rule_area() -> None:
    mod = part("U1", 15, 15, half=6, rule_areas=(tuple(rect_poly(10, 10, 10, 10)),))
    m = board(parts=(mod,), tracks=route("S1", [(12, 15), (18, 15)]))
    m13 = get(run(m), "M13")
    assert m13.verdict == "fail"
    assert m13.value == 1
    outside = board(parts=(mod,), tracks=route("S1", [(30, 15), (40, 15)]))
    assert get(run(outside), "M13").verdict == "pass"
    v = board(parts=(mod,), vias=(via("S1", (15, 15)),))
    assert get(run(v), "M13").verdict == "fail"


def test_m13_board_keepout_inside_module_courtyard() -> None:
    mod = part("U1", 15, 15, half=6)
    ko = zone(
        "ANT",
        "",
        ("F.Cu",),
        tuple(rect_poly(12, 12, 4, 4)),
        rule_area=True,
        keepout=frozenset({"tracks"}),
    )
    m = board(parts=(mod,), zones=(ko,), tracks=route("S1", [(13, 14), (15, 14)]))
    assert classify(m).part_class["U1"] == "module_with_antenna"
    assert get(run(m), "M13").verdict == "fail"
    other_layer = replace(m, tracks=route("S1", [(13, 14), (15, 14)], layer="B.Cu"))
    assert get(run(other_layer), "M13").verdict == "pass"


# ---- timing --------------------------------------------------------------------------------


def test_timing_200_parts_2000_segments() -> None:
    """Seeded 200-part board, ~1800 segments, a GND plane on In1.Cu with an antipad per via
    and 20 voids, all checks incl. the JLC preset: budget 0.5 s, asserted generously."""
    m = routed_board(11)
    assert len(m.parts) == 200
    assert len(m.tracks) >= 1500
    holes = [tuple(rect_poly(v.at[0] - 0.5, v.at[1] - 0.5, 1, 1)) for v in m.vias]
    holes += [tuple(rect_poly(5 + 4.5 * k, 60, 2, 2)) for k in range(20)]
    plane = _plane((tuple(rect_poly(0.3, 0.3, 99.4, 79.4)),), (tuple(holes),))
    m = replace(m, copper_layers=_FOUR, zones=(plane,))
    cls = classify(m)
    t0 = time.perf_counter()
    rep = compute_metrics(m, cls, BoardDoc(), drc=None, parity=None, preset=PRESETS["jlcpcb-4l"])
    elapsed = time.perf_counter() - t0
    assert rep.stage == "routed"
    assert len(rep.metrics) == 14
    assert get(rep, "M11").verdict != "n/a"
    assert elapsed < 2.0, elapsed


def test_m11_thermal_gaps_and_antipad_slot() -> None:
    """Thermal-relief gaps around one plated pad (holes that miss its centre) are not voids;
    a slot of merged antipads over two vias 6 mm apart is one."""
    full = tuple(rect_poly(0, 0, 100, 80))
    gaps = tuple(tuple(rect_poly(x, y, 0.8, 0.8)) for x, y in ((49, 29), (50.2, 29), (49, 30.2)))
    tp = part(
        "J9",
        50,
        30,
        pads=(
            with_layers(
                pad("J9", "1", "GND", (50, 30)),
                "F.Cu",
                "B.Cu",
                "In1.Cu",
                "In2.Cu",
                smd=False,
                drill=0.8,
            ),
        ),
    )
    m = board(
        copper_layers=_FOUR,
        zones=(_plane((full,), (gaps,)),),
        tracks=route("S1", [(40, 29.4), (60, 29.4)]),
        parts=(tp,),
    )
    assert not [o for o in get(run(m), "M11").offenders if "void" in o.what]
    slot = tuple(rect_poly(39, 49, 8, 2))
    vias = (via("S2", (40, 50), kiid="v1"), via("S2", (46, 50), kiid="v2"))
    m = _m11_board(_plane((full,), ((slot,),)), route("S1", [(43, 40), (43, 60)]), vias=vias)
    assert len([o for o in get(run(m), "M11").offenders if "void" in o.what]) == 1
