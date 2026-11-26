"""Round 2 (R1, spec §22.5): M12 neck-down, M11 own antipad, M3 silk-line warning, M4 without
the T clause, M15 Y-junctions, M16 doglegs / pad-entry loops and jogs."""

from __future__ import annotations

from dataclasses import replace

from copperchat.board.boardmd import BoardDoc
from copperchat.board.classify import classify
from copperchat.board.geom import rect_poly
from copperchat.board.metrics import Metric, MetricsReport, compute_metrics
from copperchat.board.model import BoardModel, Pad, Point, Zone
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
