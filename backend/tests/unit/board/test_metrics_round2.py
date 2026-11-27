"""Round 2 (R1, spec §22.5): M12 neck-down, M11 own antipad, M3 silk-line warning, M4 without
the T clause, M15 Y-junctions, M16 doglegs / pad-entry loops and jogs."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from copperchat.board import metrics
from copperchat.board.boardmd import BoardDoc
from copperchat.board.classify import classify
from copperchat.board.geom import rect_poly
from copperchat.board.metrics import Metric, MetricsReport, compute_metrics
from copperchat.board.model import ArcTrack, BoardModel, Pad, Part, Point, Zone
from copperchat.board.presets import FabPreset
from copperchat.kicad_tools.board.api import DrcMarker, DrcResult

from .builders import board, net_class, part, track, via, zone
from .builders_circuit import pad, route


def run(
    m: BoardModel,
    *,
    drc: DrcResult | None = None,
    preset: FabPreset | None = None,
) -> MetricsReport:
    return compute_metrics(m, classify(m), BoardDoc(), drc=drc, parity=None, preset=preset)


def get(rep: MetricsReport, mid: str) -> Metric:
    return next(x for x in rep.metrics if x.id == mid)


# ---- M12 neck-down -------------------------------------------------------------------------


def _qfn_board(*tracks: object, pad_w: float = 0.25) -> BoardModel:
    """U1 at (50, 40), courtyard 4 x 4 (escape zone 47.5..52.5), one 0.25 x 0.8 pad on P1
    (net class Power, 0.5 mm)."""
    u1 = part("U1", 50, 40, pads=(pad("U1", "1", "P1", (50, 40), w=pad_w, h=0.8),))
    r1 = part("R1", 70, 40, pads=(pad("R1", "1", "P1", (70, 40), w=1.0, h=1.0),))
    return board(
        parts=(u1, r1),
        tracks=tuple(tracks),
        net_classes=(net_class(), net_class("Power", 0.5)),
        net_class_of=(("P1", "Power"),),
    )


def _m12_widths(m: BoardModel) -> list[str]:
    return [o.what for o in get(run(m), "M12").offenders if o.what.startswith("track_width")]


def test_m12_neck_inside_zone_passes() -> None:
    m = _qfn_board(track("P1", start=(50, 40), end=(51.5, 40), width=0.25))
    assert _m12_widths(m) == []
    assert get(run(m), "M12").verdict == "pass"


def test_m12_neck_outside_zone_fails() -> None:
    m = _qfn_board(track("P1", start=(55, 40), end=(57, 40), width=0.25))
    assert len(_m12_widths(m)) == 1


def test_m12_neck_crossing_zone_fails() -> None:
    m = _qfn_board(track("P1", start=(51, 40), end=(54, 40), width=0.25))
    assert len(_m12_widths(m)) == 1
    assert get(run(m), "M12").verdict == "fail"


def test_m12_neck_below_min_fails() -> None:
    m = _qfn_board(track("P1", start=(50, 40), end=(51.5, 40), width=0.1))
    assert len(_m12_widths(m)) == 1
    # a 0.1 mm pad: the neck is the preset minimum (0.127), a 0.1 track stays too narrow
    m = _qfn_board(track("P1", start=(50, 40), end=(51.5, 40), width=0.1), pad_w=0.1)
    assert len(_m12_widths(m)) == 1
    m = _qfn_board(track("P1", start=(50, 40), end=(51.5, 40), width=0.127), pad_w=0.1)
    assert _m12_widths(m) == []


def test_m12_neck_needs_pad_on_the_track_layer() -> None:
    # a narrow B.Cu piece under the F.Cu-only fine-pitch part is no neck-down
    m = _qfn_board(track("P1", "B.Cu", start=(50, 40), end=(51.5, 40), width=0.25))
    assert len(_m12_widths(m)) == 1


def test_m12_neck_arc_bulging_out_of_zone_fails() -> None:
    # ends and mid inside the zone (x >= 47.5), the bulge reaches x ~ 47.19
    arc = ArcTrack("arc", "P1", "F.Cu", (47.6, 39.0), (47.6, 41.0), (49.6, 41.0), 0.25, False)
    m = replace(_qfn_board(), arcs=(arc,))
    assert len(_m12_widths(m)) == 1
    inside = ArcTrack("arc", "P1", "F.Cu", (49.0, 39.0), (49.6, 39.6), (50.0, 40.6), 0.25, False)
    assert _m12_widths(replace(_qfn_board(), arcs=(inside,))) == []


def test_m12_many_parts_and_narrow_tracks_fast() -> None:
    import time

    parts = tuple(
        part(
            f"C{i}",
            2 + (i % 40) * 2.4,
            2 + (i // 40) * 5.0,
            half=1.0,
            pads=(pad(f"C{i}", "1", "P1", (2 + (i % 40) * 2.4, 2 + (i // 40) * 5.0)),),
        )
        for i in range(500)
    )
    tracks = tuple(
        track(
            "P1", start=(1.0 + k * 0.04, 79.0), end=(1.0 + k * 0.04, 79.5), width=0.2, kiid=f"t{k}"
        )
        for k in range(2000)
    )
    m = board(
        parts=parts,
        tracks=tracks,
        net_classes=(net_class(), net_class("Power", 0.5)),
        net_class_of=(("P1", "Power"),),
    )
    t0 = time.perf_counter()
    rep = compute_metrics(
        m, classify(m), BoardDoc(), drc=None, parity=None, preset=None, only=frozenset({"M12"})
    )
    assert time.perf_counter() - t0 < 1.0
    assert get(rep, "M12").value == 2000


def test_m12_neck_needs_own_net_pad() -> None:
    # the zone of a part whose narrow pad is on ANOTHER net does not excuse the track
    u1 = part("U1", 50, 40, pads=(pad("U1", "1", "P2", (50, 40), w=0.25, h=0.8),))
    m = replace(
        _qfn_board(track("P1", start=(50, 41), end=(51.5, 41), width=0.25)),
        parts=(u1, part("R1", 70, 40, pads=(pad("R1", "1", "P1", (70, 40)),))),
    )
    assert len(_m12_widths(m)) == 1


# ---- M11 own antipad -----------------------------------------------------------------------

_FOUR = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")


def _plane_with_hole(hole: tuple[Point, ...]) -> Zone:
    z = zone("GNDP", "GND", ("In1.Cu",), tuple(rect_poly(0, 0, 100, 80)))
    return replace(z, filled=(tuple(rect_poly(0, 0, 100, 80)),), filled_holes=((hole,),))


def _crossings(m: BoardModel) -> list[str]:
    return [o.what for o in get(run(m), "M11").offenders if "void" in o.what]


def test_m11_own_via_antipad_not_crossing() -> None:
    # a 2.4 mm square hole around the S1 via: corners 1.7 mm away > reach 1.3 -> a void
    hole = tuple(rect_poly(48.8, 28.8, 2.4, 2.4))
    gnd = part("R9", 10, 10, pads=(pad("R9", "1", "GND", (10, 10)),))
    vias = (via("S1", (50, 30), 0.6, kiid="v-s1"),)
    own = route("S1", [(50, 30), (60, 30)])
    m = board(copper_layers=_FOUR, zones=(_plane_with_hole(hole),), tracks=own, vias=vias)
    m = replace(m, parts=(*m.parts, gnd))
    assert _crossings(m) == []
    # a foreign net's track over the same hole is still a crossing
    foreign = route("S2", [(45, 30.5), (55, 30.5)])
    m2 = replace(m, tracks=(*own, *foreign))
    got = _crossings(m2)
    assert len(got) == 1 and "S2" in got[0]
    # the own track leaving the disc while still over the hole (along the diagonal) crosses
    diag = route("S1", [(50, 30), (52, 32)])
    assert len(_crossings(replace(m, tracks=diag))) == 1


def test_m11_own_plated_pad_antipad_not_crossing() -> None:
    hole = tuple(rect_poly(48.8, 28.8, 2.4, 2.4))
    tht = Pad("J1", "1", "S1", 50, 30, 0.6, 0.6, 0, "circle", frozenset({"*.Cu"}), False, 0.3)
    m = board(
        copper_layers=_FOUR,
        zones=(_plane_with_hole(hole),),
        tracks=route("S1", [(50, 30), (60, 30)]),
        parts=(
            part("J1", 50, 30, pads=(tht,)),
            part("R9", 10, 10, pads=(pad("R9", "1", "GND", (10, 10)),)),
        ),
    )
    assert _crossings(m) == []


# ---- M3 silk-line warning ------------------------------------------------------------------


def _silk_board() -> BoardModel:
    r1 = part("R1", 30, 30, item_kiids=(("k-line", "shape"), ("k-ref", "reference")))
    return board(parts=(r1, part("R2", 60, 50)))


def _marker(*kiids: str, severity: str = "error") -> DrcMarker:
    return DrcMarker(
        type="text_thickness",
        severity=severity,
        text="Text thickness 0.12 < 0.15",
        kiids=kiids,
        pos=(1.0, 2.0),
    )  # type: ignore[arg-type]


def test_m3_silk_line_warning_only() -> None:
    drc = DrcResult(markers=(_marker("k-line"),), unconnected=0, seconds=0.1)
    m3 = get(run(_silk_board(), drc=drc), "M3")
    assert m3.value == 0
    assert m3.verdict == "review"
    assert [o.what.split(":")[0] for o in m3.offenders] == ["silk_line_warning"]
    assert m3.offenders[0].refs == ("R1",)


def test_m3_text_marker_stays_hard() -> None:
    drc = DrcResult(markers=(_marker("k-ref"),), unconnected=0, seconds=0.1)
    m3 = get(run(_silk_board(), drc=drc), "M3")
    assert m3.value == 1 and m3.verdict == "fail"
    # a marker naming a silk line AND a text item of the same footprint stays hard
    drc = DrcResult(markers=(_marker("k-line", "k-ref"),), unconnected=0, seconds=0.1)
    m3 = get(run(_silk_board(), drc=drc), "M3")
    assert m3.value == 1 and m3.verdict == "fail"
    # the footprint's own KIID (no item role) is not a silk line either
    drc = DrcResult(markers=(_marker("kiid-R1"),), unconnected=0, seconds=0.1)
    assert get(run(_silk_board(), drc=drc), "M3").value == 1


# ---- M4 without the T clause, M15 ----------------------------------------------------------


def _t_board(branch_end: Point) -> BoardModel:
    return board(
        tracks=(
            track("N1", start=(10.0, 10.0), end=(20.0, 10.0), kiid="a"),
            track("N1", start=(15.0, 10.0), end=branch_end, kiid="b"),
        )
    )


def test_m4_t_junction_not_hard() -> None:
    rep = run(_t_board((15.0, 15.0)))
    assert get(rep, "M4").verdict == "pass"
    assert get(rep, "M15").verdict == "pass"


def test_m4_degree2_ninety_still_fails() -> None:
    m = board(
        tracks=(
            track("N1", start=(10.0, 10.0), end=(15.0, 10.0), kiid="a"),
            track("N1", start=(15.0, 10.0), end=(15.0, 15.0), kiid="b"),
        )
    )
    m4 = get(run(m), "M4")
    assert m4.verdict == "fail"
    assert [o.what for o in m4.offenders] == ["ninety"]


def test_m15_y_review_when_switch_off() -> None:
    rep = run(_t_board((18.0, 13.0)))
    m15 = get(rep, "M15")
    assert m15.verdict == "review"
    assert m15.value == 1
    assert [(o.what, o.nets, o.at) for o in m15.offenders] == [
        ("y_junction", ("N1",), (15.0, 10.0))
    ]
    assert get(rep, "M4").verdict == "pass"


def test_m15_y_fails_when_switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics, "M15_HARD", True)
    assert get(run(_t_board((18.0, 13.0))), "M15").verdict == "fail"
    assert get(run(_t_board((15.0, 15.0))), "M15").verdict == "pass"


def test_switches_default_review(monkeypatch: pytest.MonkeyPatch) -> None:
    assert metrics.M15_HARD is False and metrics.M16_HARD is False
    y = run(_t_board((18.0, 13.0)))
    t = run(_t_board((15.0, 15.0)))
    assert y.hard_failures == t.hard_failures
    assert get(y, "M15").verdict == "review"
    # switched off: no hard threshold text (the e2e golden pins hard values exactly)
    for mid in ("M15", "M16"):
        x = get(y, mid)
        assert x.hard is None
        assert x.review is not None and "hard from round 2 R4" in x.review
    monkeypatch.setattr(metrics, "M15_HARD", True)
    monkeypatch.setattr(metrics, "M16_HARD", True)
    on = run(_t_board((18.0, 13.0)))
    assert get(on, "M15").hard == metrics.THRESHOLD_TEXT["M15"][0]
    assert get(on, "M16").hard == metrics.THRESHOLD_TEXT["M16"][0]


def test_metric_ids_m1_to_m16() -> None:
    assert tuple(f"M{i}" for i in range(1, 17)) == metrics.METRIC_IDS
    rep = run(board())
    assert [x.id for x in rep.metrics] == [f"M{i}" for i in range(1, 17)]
    assert set(metrics.THRESHOLD_TEXT) == set(metrics.METRIC_IDS)


# ---- M16 -----------------------------------------------------------------------------------


def _pair(d_dy: float = 0.0, *extra: object, d_x: float = 34.0, **kw: object) -> BoardModel:
    """R1 (pads 1.0 x 1.3 at (29, 30) on A and (31, 30) on N1) and D1 (pads at (d_x, 30 +
    d_dy) on N1 and (d_x + 2, 30 + d_dy) on B): R1.2-D1.1 are adjacent (gap 2.0 mm)."""
    r1 = part(
        "R1",
        30,
        30,
        pads=(
            pad("R1", "1", "A", (29, 30), w=1.0, h=1.3),
            pad("R1", "2", "N1", (31, 30), w=1.0, h=1.3),
        ),
    )
    d1 = part(
        "D1",
        d_x + 1,
        30 + d_dy,
        pads=(
            pad("D1", "1", "N1", (d_x, 30 + d_dy), w=1.0, h=1.3),
            pad("D1", "2", "B", (d_x + 2, 30 + d_dy), w=1.0, h=1.3),
        ),
    )
    parts = (r1, d1, *(p for p in extra if isinstance(p, Part)))
    return board(parts=parts, **kw)


# a routed track on net A elsewhere so the board is at the routed stage
_STAGE = route("A", [(29, 30), (29, 20)])


def _m16(m: BoardModel) -> Metric:
    return get(run(m), "M16")


def _whats(m16: Metric) -> list[str]:
    return [o.what.split(" ")[0] for o in m16.offenders]


def test_m16_dogleg() -> None:
    dog = route("N1", [(31, 30), (32, 30), (32.5, 30.5), (34, 30.5)])
    m16 = _m16(_pair(0.5, tracks=(*_STAGE, *dog)))
    assert "dogleg" in _whats(m16)
    assert m16.value >= 1
    assert m16.verdict == "review"  # M16_HARD off
    dog_o = next(o for o in m16.offenders if o.what.startswith("dogleg"))
    assert dog_o.refs == ("D1", "R1") and dog_o.nets == ("N1",)


def test_m16_dogleg_fails_when_switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics, "M16_HARD", True)
    dog = route("N1", [(31, 30), (32, 30), (32.5, 30.5), (34, 30.5)])
    assert _m16(_pair(0.5, tracks=(*_STAGE, *dog))).verdict == "fail"


def test_m16_straight_ok() -> None:
    m16 = _m16(_pair(0.5, tracks=(*_STAGE, *route("N1", [(31, 30.25), (34, 30.25)]))))
    assert m16.offenders == () and m16.verdict == "pass"
    m16 = _m16(_pair(0.0, tracks=(*_STAGE, *route("N1", [(31, 30), (34, 30)]))))
    assert m16.offenders == ()


def test_m16_via_detour_is_dogleg() -> None:
    # straight in x but dipping to B.Cu through two vias: not one segment on a shared
    # layer; the vias sit in the corridor but are the net's own, so it stays free
    tracks = (
        *_STAGE,
        *route("N1", [(31, 30), (32.0, 30)]),
        *route("N1", [(32.0, 30), (33.0, 30)], layer="B.Cu"),
        *route("N1", [(33.0, 30), (34, 30)]),
    )
    vias = (via("N1", (32.0, 30), kiid="v1"), via("N1", (33.0, 30), kiid="v2"))
    assert "dogleg" in _whats(_m16(_pair(0.0, tracks=tracks, vias=vias)))
    # a foreign via in the corridor blocks it
    foreign = (via("X", (32.5, 30), kiid="vx"),)
    m = _pair(0.5, tracks=(*_STAGE, *route("N1", [(31, 30), (32, 30), (32.5, 30.5), (34, 30.5)])))
    assert "dogleg" not in _whats(_m16(replace(m, vias=foreign)))


def test_m16_unrouted_pair_not_dogleg() -> None:
    m16 = _m16(_pair(0.5, tracks=_STAGE))
    assert m16.offenders == ()


def test_m16_straight_with_t_branch_ok() -> None:
    c1 = part(
        "C1",
        32.5,
        36,
        pads=(pad("C1", "1", "N1", (32.5, 35), w=1.0, h=1.0), pad("C1", "2", "GND", (32.5, 37))),
    )
    tracks = (
        *_STAGE,
        *route("N1", [(31, 30), (34, 30)]),
        *route("N1", [(32.5, 30), (32.5, 35)]),
    )
    m16 = _m16(_pair(0.0, c1, tracks=tracks))
    assert "dogleg" not in _whats(m16), m16.offenders
    assert m16.verdict == "pass", m16.offenders


def test_m16_tht_pair_straight_on_bottom_ok() -> None:
    tht = frozenset({"*.Cu", "*.Mask"})

    def tp(ref: str, at: Point, net: str) -> Pad:
        return Pad(ref, "1", net, at[0], at[1], 1.7, 1.7, 0.0, "circle", tht, False, 1.0)

    j1 = part("J1", 30, 50, pads=(tp("J1", (30, 50), "N1"),))
    j2 = part("J2", 33, 50, pads=(tp("J2", (33, 50), "N1"),))
    m = board(parts=(j1, j2), tracks=route("N1", [(30, 50), (33, 50)], layer="B.Cu"))
    m16 = _m16(m)
    assert m16.offenders == (), m16.offenders
    # the same pair joined by a dogleg on B.Cu is one
    dog = route("N1", [(30, 50), (31, 50), (31.5, 50.5), (32, 50.5), (32.5, 50), (33, 50)])
    m = board(parts=(j1, j2), tracks=dog)
    assert "dogleg" in _whats(_m16(m))


def test_m16_straight_on_blocked_shared_layer_ok() -> None:
    # THT pair: F.Cu corridor blocked by a foreign F.Cu pad, B.Cu free; a straight F.Cu
    # link (beside the blocker) is still one straight segment on a shared layer
    tht = frozenset({"*.Cu", "*.Mask"})

    def tp(ref: str, at: Point) -> Pad:
        return Pad(ref, "1", "N1", at[0], at[1], 1.7, 1.7, 0.0, "circle", tht, False, 1.0)

    j1 = part("J1", 30, 50, pads=(tp("J1", (30, 50)),))
    j2 = part("J2", 33, 50, pads=(tp("J2", (33, 50)),))
    x1 = part("X1", 31.5, 49.75, pads=(pad("X1", "1", "X", (31.5, 49.75), w=0.3, h=0.3),))
    m = board(parts=(j1, j2, x1), tracks=route("N1", [(30, 50.6), (33, 50.6)]))
    assert "dogleg" not in _whats(_m16(m))
    dog = route("N1", [(30, 50.6), (31, 50.6), (31.3, 50.3), (33, 50.3)])
    assert "dogleg" in _whats(_m16(replace(m, tracks=dog)))  # B.Cu corridor free


def test_m16_corridor_blocked_by_foreign_pad_not_dogleg() -> None:
    x1 = part("X1", 32.5, 30.25, pads=(pad("X1", "1", "X", (32.5, 30.25), w=0.4, h=0.4),))
    dog = route("N1", [(31, 30), (32, 30), (32.5, 30.5), (34, 30.5)])
    m16 = _m16(_pair(0.5, x1, tracks=(*_STAGE, *dog)))
    assert "dogleg" not in _whats(m16)
    # a track keepout over the corridor blocks it too
    ko = replace(
        zone("KO", "", ("F.Cu",), tuple(rect_poly(32, 29, 1, 3))),
        rule_area=True,
        keepout=frozenset({"tracks"}),
    )
    m16 = _m16(_pair(0.5, tracks=(*_STAGE, *dog), zones=(ko,)))
    assert "dogleg" not in _whats(m16)


def test_m16_corridor_clearance_from_pattern_class() -> None:
    # a foreign pad 0.8 mm beside the corridor: free with the Default clearance 0.2
    # (reach 0.1 + 0.2), blocked by the pattern-assigned class clearance 0.8 (reach 0.9)
    x1 = part("X1", 32.5, 31.0, pads=(pad("X1", "1", "X", (32.5, 31.0), w=0.4, h=0.4),))
    dog = route("N1", [(31, 30), (32, 30), (32.5, 30.5), (34, 30.5)])
    base = _pair(0.0, x1, tracks=(*_STAGE, *dog))
    assert "dogleg" in _whats(_m16(base))
    wide = net_class("Wide", 0.2, clearance=0.8, patterns=("N*",))
    m = replace(
        base,
        net_classes=(net_class(), wide),
        net_class_of=(("N1", "Effective for net: N1"),),
    )
    assert "dogleg" not in _whats(_m16(m))


def _far_pad(ref: str, net: str, at: Point) -> Part:
    return part(ref, at[0], at[1], pads=(pad(ref, "1", net, at, w=1.0, h=1.0),))


def _walk(start: Point, heading: float, steps: list[tuple[float, float]]) -> list[Point]:
    import math

    pts = [start]
    h = heading
    for turn, length in steps:
        h += turn
        x, y = pts[-1]
        pts.append((x + length * math.cos(math.radians(h)), y + length * math.sin(math.radians(h))))
    return [(round(x, 6), round(y, 6)) for x, y in pts]


def test_m16_loop() -> None:
    pts = _walk((50, 50), 0, [(0, 0.8), (45, 0.4), (45, 0.4), (45, 0.4), (0, 6.0)])
    m = board(
        parts=(_far_pad("U1", "N2", (50, 50)), _far_pad("U2", "N2", pts[-1])),
        tracks=route("N2", pts),
    )
    m16 = _m16(m)
    assert _whats(m16) == ["pad_loop"]
    assert m16.value == 1 and m16.verdict == "review"
    assert m16.offenders[0].refs == ("U1",)


def test_m16_loop_counted_once_per_pad() -> None:
    # U2 and U3 lie far apart and both nearest to U1: the MST joins U1-U2 and U1-U3, and
    # both paths leave U1 through the same looping entry piece
    pts = _walk((50, 50), 0, [(0, 0.8), (45, 0.4), (45, 0.4), (45, 0.4), (0, 6.0)])
    hub = pts[-1]
    tracks = (
        *route("N2", pts),
        *route("N2", [hub, (30.0, 60.0)]),
        *route("N2", [hub, (70.0, 60.0)]),
    )
    m = board(
        parts=(
            _far_pad("U1", "N2", (50, 50)),
            _far_pad("U2", "N2", (30.0, 60.0)),
            _far_pad("U3", "N2", (70.0, 60.0)),
        ),
        tracks=tracks,
    )
    pairs = {tuple(sorted((a.ref, b.ref))) for a, b in metrics.connections(m, "N2")}
    assert pairs == {("U1", "U2"), ("U1", "U3")}
    m16 = _m16(m)
    assert _whats(m16) == ["pad_loop"]
    assert m16.value == 1


def test_m16_loop_at_b_end_uses_b_width() -> None:
    # the loop sits at the far pad; the path changes width on the way (0.2 -> 0.5): b's
    # entry zone is sized by the 0.5 piece (pad edge + 1.5 mm = 2.0 from the centre), so
    # the loop 1.7-1.9 mm out is inside it (a 0.2-sized zone ends at 1.5 mm)
    loop = _walk((60, 50), 180, [(0, 1.7), (45, 0.25), (45, 0.25), (45, 0.25), (0, 3.0)])
    a_end = loop[-1]
    tracks = (
        *route("N2", [(a_end[0] - 6.0, a_end[1]), a_end], width=0.2),
        *route("N2", list(reversed(loop)), width=0.5),
    )
    m = board(
        parts=(_far_pad("U1", "N2", (a_end[0] - 6.0, a_end[1])), _far_pad("U2", "N2", (60, 50))),
        tracks=tracks,
    )
    m16 = _m16(m)
    assert "pad_loop" in _whats(m16)
    assert next(o for o in m16.offenders if o.what.startswith("pad_loop")).refs == ("U2",)


def test_m16_jog_review_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics, "M16_HARD", True)
    pts = _walk((50, 50), 0, [(0, 0.6), (45, 0.4), (-45, 6.0)])
    m = board(
        parts=(_far_pad("U1", "N2", (50, 50)), _far_pad("U2", "N2", pts[-1])),
        tracks=route("N2", pts),
    )
    m16 = _m16(m)
    assert _whats(m16) == ["pad_jog"]
    assert m16.value == 0 and m16.verdict == "review"


def test_m16_ground_nets_excluded() -> None:
    pts = _walk((50, 50), 0, [(0, 0.6), (45, 0.4), (-45, 6.0)])
    m = board(
        parts=(_far_pad("U1", "GND", (50, 50)), _far_pad("U2", "GND", pts[-1])),
        tracks=(*route("GND", pts), *route("N2", [(10, 10), (20, 10)])),
    )
    assert _m16(m).offenders == ()


def test_m16_alignable_placement_stage() -> None:
    m16 = _m16(_pair(1.2))  # y overlap 0.1 < 0.2: a 0.1 mm shift gives a corridor
    assert _whats(m16) == ["alignable"]
    assert m16.verdict == "review" and m16.value == 0
    assert m16.offenders[0].value == pytest.approx(0.1)
    assert _m16(_pair(0.5)).offenders == ()  # a corridor already exists
    assert _m16(_pair(3.0)).offenders == ()  # needs 1.9 mm > 1 mm
    assert _m16(_pair(1.2, d_x=36.0)).offenders == ()  # gap 4 mm: not adjacent


# ---- routed_path ---------------------------------------------------------------------------


def _two_pads(a: Point, b: Point, net: str = "N1") -> tuple[Part, Part]:
    return _far_pad("U1", net, a), _far_pad("U2", net, b)


def test_routed_path_straight_and_order() -> None:
    u1, u2 = _two_pads((10, 10), (20, 10))
    m = board(parts=(u1, u2), tracks=route("N1", [(20, 10), (15, 10), (10, 10)]))
    got = metrics.routed_path(m, u1.pads[0], u2.pads[0])
    assert got == (("F.Cu", (10, 10), (15, 10)), ("F.Cu", (15, 10), (20, 10)))
    back = metrics.routed_path(m, u2.pads[0], u1.pads[0])
    assert back == (("F.Cu", (20, 10), (15, 10)), ("F.Cu", (15, 10), (10, 10)))
    assert sum(math.dist(p, q) for _, p, q in got or ()) == metrics.routed_length(
        m, u1.pads[0], u2.pads[0]
    )


def test_routed_path_via_skipped_and_unconnected() -> None:
    u1, u2 = _two_pads((10, 10), (20, 10))
    u2 = replace(u2, pads=(replace(u2.pads[0], layers=frozenset({"B.Cu"})),))
    tracks = (*route("N1", [(10, 10), (15, 10)]), *route("N1", [(15, 10), (20, 10)], layer="B.Cu"))
    m = board(parts=(u1, u2), tracks=tracks, vias=(via("N1", (15, 10)),))
    got = metrics.routed_path(m, u1.pads[0], u2.pads[0])
    assert got == (("F.Cu", (10, 10), (15, 10)), ("B.Cu", (15, 10), (20, 10)))
    m = board(parts=(u1, u2), tracks=tracks)  # no via: not connected
    assert metrics.routed_path(m, u1.pads[0], u2.pads[0]) is None
    other = _far_pad("U3", "N9", (30, 10))
    assert metrics.routed_path(m, u1.pads[0], other.pads[0]) is None


def test_routed_path_arc_as_two_chords() -> None:
    u1, u2 = _two_pads((10, 10), (20, 10))
    arc = ArcTrack("arc", "N1", "F.Cu", (10, 10), (15, 15), (20, 10), 0.2, False)
    m = board(parts=(u1, u2), arcs=(arc,))
    got = metrics.routed_path(m, u1.pads[0], u2.pads[0])
    assert got == (("F.Cu", (10, 10), (15, 15)), ("F.Cu", (15, 15), (20, 10)))
