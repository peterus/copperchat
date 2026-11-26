"""B-C7 metrics, rules side: M2/M3 (DRC, parity, accepted warnings), M13 edges, M14 fab rules,
offender capping, `only`, thresholds."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace

import pytest

from copperchat.board import metrics
from copperchat.board.boardmd import AcceptedWarning, BoardDoc, EdgeRow
from copperchat.board.classify import Classification, classify
from copperchat.board.metrics import HARD, REVIEW, Metric, MetricsReport, compute_metrics
from copperchat.board.model import BoardModel, GraphicItem, Pad, Part, TextItem
from copperchat.board.presets import PRESETS, FabPreset
from copperchat.kicad_tools.board.api import DrcMarker, DrcResult

from .builders import board, net_class, part, track, via, with_part
from .builders_circuit import pad, with_layers


def run(
    m: BoardModel,
    *,
    cls: Classification | None = None,
    doc: BoardDoc | None = None,
    drc: DrcResult | None = None,
    parity: tuple[DrcMarker, ...] | None = None,
    preset: FabPreset | None = None,
    only: frozenset[str] | None = None,
) -> MetricsReport:
    return compute_metrics(
        m,
        cls if cls is not None else classify(m),
        doc if doc is not None else BoardDoc(),
        drc=drc,
        parity=parity,
        preset=preset,
        only=only,
    )


def get(rep: MetricsReport, mid: str) -> Metric:
    return next(x for x in rep.metrics if x.id == mid)


def test_no_kipy_import() -> None:
    code = "import sys, copperchat.board.metrics;assert 'kipy' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)


# ---- M2 / M3 -------------------------------------------------------------------------------


def marker(type_: str, severity: str = "error", *kiids: str) -> DrcMarker:
    return DrcMarker(
        type=type_, severity=severity, text=f"{type_} text", kiids=kiids, pos=(1.0, 2.0)
    )  # type: ignore[arg-type]


def test_m2_m3_from_drc() -> None:
    m = board()  # parts R1 (kiid-R1), R2 (kiid-R2)
    drc = DrcResult(
        markers=(
            marker("clearance", "error", "kiid-R1"),
            marker("unconnected_items", "error", "kiid-R1"),
            marker("silk_overlap", "warning", "kiid-R1"),
            marker("DRCE_LIB_FOOTPRINT_MISMATCH", "warning", "kiid-R2"),
            marker("clearance", "exclusion", "kiid-R2"),
        ),
        unconnected=1,
        seconds=0.1,
    )
    doc = BoardDoc(accepted=(AcceptedWarning("silk_overlap", ("R1",), "logo", "owner"),))
    rep = run(m, drc=drc, doc=doc)
    m2 = get(rep, "M2")
    assert m2.value == 1
    assert m2.verdict == "fail"
    m3 = get(rep, "M3")
    assert m3.value == 2
    assert m3.verdict == "fail"
    accepted = [o for o in m3.offenders if o.what.startswith("accepted")]
    assert len(accepted) == 1
    assert accepted[0].refs == ("R1",)
    # without the error and the foreign warning only the accepted one is left -> review
    only_acc = replace(drc, markers=drc.markers[2:3], unconnected=0)
    rep = run(m, drc=only_acc, doc=doc)
    assert get(rep, "M3").verdict == "review"
    assert get(rep, "M3").value == 0
    assert get(rep, "M2").verdict == "pass"
    # refs not covered by the row's items -> not accepted
    wide = replace(drc, markers=(marker("silk_overlap", "warning", "kiid-R1", "kiid-R2"),))
    assert get(run(m, drc=wide, doc=doc), "M3").value == 1


def test_m2_m3_na_without_drc() -> None:
    rep = run(board())
    assert get(rep, "M2").verdict == "n/a"
    assert get(rep, "M3").verdict == "n/a"


def test_m3_parity_counts() -> None:
    hole = part("H1", 5, 5, lib_id="MountingHole:MountingHole_3.2mm")
    m = board(parts=(part("R1", 30, 30), hole))
    drc = DrcResult(markers=(), unconnected=0, seconds=0.1)
    parity = (
        marker("missing_footprint", "error", "kiid-R1"),
        marker("not_in_schematic", "error", "kiid-H1"),
    )
    m3 = get(run(m, drc=drc, parity=parity), "M3")
    assert m3.value == 1
    assert m3.verdict == "fail"
    assert get(run(m, drc=drc, parity=None), "M3").value == 0
    # a non-hole part not in the schematic counts
    extra = (marker("not_in_schematic", "error", "kiid-R1"),)
    assert get(run(m, drc=drc, parity=extra), "M3").value == 1


# ---- M13 edges -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("x", "verdict"),
    [(2.4, "pass"), (2.6, "fail"), (1.0, "pass")],  # courtyard x - 2: 0.4 / 0.6 / overhang 1
)
def test_m13_edge_row(x: float, verdict: str) -> None:
    m = board(parts=(part("J1", x, 40),))
    doc = BoardDoc(edges=(EdgeRow("J1", "left", "left edge", "cable"),))
    assert get(run(m, doc=doc), "M13").verdict == verdict


def test_m13_edge_row_other_edges_and_unknown_ref() -> None:
    m = board(parts=(part("J1", 50, 77.6), part("J2", 97.6, 40)))
    doc = BoardDoc(
        edges=(
            EdgeRow("J1", "bottom", "", ""),
            EdgeRow("J2", "right", "", ""),
            EdgeRow("J3", "top", "", ""),
            EdgeRow("J1", None, "anywhere", ""),
        )
    )
    m13 = get(run(m, doc=doc), "M13")
    assert m13.verdict == "fail"
    assert [o.refs for o in m13.offenders] == [("J3",)]


# ---- M14 -----------------------------------------------------------------------------------

JLC4 = PRESETS["jlcpcb-4l"]


def smd(ref: str, at: tuple[float, float], *layers: str) -> Part:
    p = with_layers(pad(ref, "1", "S1", at, w=1.0, h=1.0), *layers)
    return part(ref, at[0], at[1], pads=(p,), texts=())


def hole(ref: str, at: tuple[float, float], drill: float, *layers: str) -> Part:
    p = Pad(
        ref, "1", "", at[0], at[1], drill, drill, 0.0, "circle", frozenset(layers), False, drill
    )
    return part(ref, at[0], at[1], pads=(p,), texts=())


def silk(at: tuple[float, float], size: float = 1.0, text: str = "X") -> TextItem:
    return TextItem("text", text, "F.SilkS", at, (size, size), 0.15, True)


GOOD_PAD = ("F.Cu", "F.Paste", "F.Mask")
U5 = smd("U5", (40, 40), *GOOD_PAD)


def _add(m: BoardModel, **kw: object) -> BoardModel:
    out = m
    for k, v in kw.items():
        out = replace(out, **{k: (*getattr(out, k), *v)})  # type: ignore[arg-type]
    return out


CASES: dict[str, tuple[Callable[[BoardModel], BoardModel], str]] = {
    "track": (
        lambda m: _add(m, tracks=(track("S1", "F.Cu", (40, 60), (50, 60), 0.12),)),
        "track_width",
    ),
    "via_drill": (lambda m: _add(m, vias=(via("S1", (40, 60), 0.6, 0.25),)), "via_drill"),
    "annular": (lambda m: _add(m, vias=(via("S1", (40, 60), 0.6, 0.32),)), "annular_ring"),
    "npth": (lambda m: _add(m, parts=(hole("H1", (70, 60), 0.4),)), "npth_drill"),
    "pth": (lambda m: _add(m, parts=(hole("H1", (70, 60), 0.15, "F.Cu", "B.Cu"),)), "pth_drill"),
    "copper_edge": (
        lambda m: _add(m, tracks=(track("S1", "F.Cu", (40, 0.35), (50, 0.35), 0.2),)),
        "copper_to_edge",
    ),
    "hole_edge": (lambda m: _add(m, parts=(hole("H1", (0.75, 40), 1.0),)), "hole_to_edge"),
    "silk_text": (lambda m: _add(m, texts=(silk((70, 20), 0.8),)), "silk_text_height"),
    "silk_line": (
        lambda m: _add(m, texts=(replace(silk((70, 20)), thickness=0.1),)),
        "silk_line_width",
    ),
    "silk_text_pad": (lambda m: _add(m, texts=(silk((40, 40.3)),)), "silk_over_pad"),
    "silk_graphic_pad": (
        lambda m: _add(
            m, graphics=(GraphicItem("F.SilkS", "segment", ((38, 41), (42, 39)), 0.15),)
        ),
        "silk_over_pad",
    ),
    "paste": (lambda m: _add(m, parts=(smd("U6", (60, 20), "F.Cu", "F.Mask"),)), "paste_missing"),
    "board_rule": (
        lambda m: replace(m, rules=(("min_track_width", 0.1), ("min_clearance", 0.127))),
        "board_rule",
    ),
}


def _m14_base() -> BoardModel:
    return board(parts=(*board().parts, U5))


def test_m14_base_passes() -> None:
    m14 = get(run(_m14_base(), preset=JLC4), "M14")
    assert m14.verdict == "pass", m14.offenders
    assert m14.value == 0


@pytest.mark.parametrize("case", sorted(CASES))
def test_m14_each_rule(case: str) -> None:
    change, what = CASES[case]
    m14 = get(run(change(_m14_base()), preset=JLC4), "M14")
    assert m14.verdict == "fail"
    assert any(o.what.startswith(what) for o in m14.offenders), m14.offenders


def test_m14_npth_hole_is_not_copper() -> None:
    m = CASES["hole_edge"][0](_m14_base())
    m14 = get(run(m, preset=JLC4), "M14")
    assert not [o for o in m14.offenders if o.what.startswith("copper_to_edge")]


def test_m14_silk_beside_pad_ok() -> None:
    # mask opening of U5: x 39.5..40.5, y 39.5..40.5
    text = silk((41.1, 40))  # bbox x 40.6..41.6: 0.1 mm clear
    line = GraphicItem("F.SilkS", "segment", ((38, 40.675), (42, 40.675)), 0.15)  # 0.1 mm clear
    back = TextItem("text", "X", "B.SilkS", (40, 40), (1.0, 1.0), 0.15, True)  # other side
    hidden = replace(silk((40, 40)), visible=False)
    m = _add(_m14_base(), texts=(text, back, hidden), graphics=(line,))
    m14 = get(run(m, preset=JLC4), "M14")
    assert m14.verdict == "pass", m14.offenders


@pytest.mark.parametrize("x", [0.8, 0.85])
def test_m14_hole_edge_at_0_3_ok(x: float) -> None:
    m = _add(_m14_base(), parts=(hole("H1", (x, 40), 1.0),))
    m14 = get(run(m, preset=JLC4), "M14")
    assert not [o for o in m14.offenders if "hole_to_edge" in o.what]
    assert m14.verdict == "pass", m14.offenders


def test_m14_custom_rule_exception() -> None:
    m = _add(_m14_base(), parts=(smd("U6", (60, 20), "F.Cu", "F.Mask"),))
    dru = (
        '(version 1)\n(rule "u6_no_paste"\n  (condition "A.Reference == \'U6\'")\n'
        "  (constraint hole_clearance (min 0.1mm)))\n"
    )
    m14 = get(run(replace(m, dru_text=dru), preset=JLC4), "M14")
    assert m14.verdict == "review"
    assert m14.value == 0
    assert any("exception" in o.what and o.refs == ("U6",) for o in m14.offenders)
    # a rule naming another ref does not cover U6
    other = dru.replace("U6", "U66")
    assert get(run(replace(m, dru_text=other), preset=JLC4), "M14").verdict == "fail"


def _excepted_regex(refs: tuple[str, ...], conditions: list[str]) -> bool:
    """The original per-ref regex matching that `_excepted` must keep."""
    import re

    return bool(refs) and all(
        any(re.search(rf"(?<![\w.]){re.escape(r)}(?!\w)", c) for c in conditions) for r in refs
    )


def test_m14_excepted_matches_regex_semantics() -> None:
    conds = [
        "A.Reference == 'U6'",
        "A.U7 == 'x'",  # preceded by '.': not a ref mention
        "A.Reference == 'R1' || B.Reference == 'C10_2'",
        "B.Reference == 'Ü1'",
        "A.Reference == '#PWR01' && x",
        "A.Reference == 'R-3'",
    ]
    cases = ["U6", "U7", "R1", "R10", "C10", "C10_2", "Ü1", "#PWR01", "PWR01", "R-3", "R", "", "6"]
    c = metrics._Conditions(conds)
    for ref in cases:
        o = metrics.Offender("x", (ref,), (), None, None)
        assert metrics._excepted(o, c) == _excepted_regex((ref,), conds), ref
    o = metrics.Offender("x", ("U6", "R1"), (), None, None)
    assert metrics._excepted(o, c)
    assert not metrics._excepted(metrics.Offender("x", ("U6", "U7"), (), None, None), c)
    assert not metrics._excepted(metrics.Offender("x", (), (), None, None), c)


def test_m14_excepted_timing_10k_offenders_1000_conditions() -> None:
    import time

    conds = [f"A.Reference == 'U{k}' && B.NetClass == 'P{k}'" for k in range(1000)]
    offs = [metrics.Offender("x", (f"U{k}", f"C{k}"), (), None, None) for k in range(10_000)]
    t0 = time.perf_counter()
    c = metrics._Conditions(conds)
    got = [metrics._excepted(o, c) for o in offs]
    elapsed = time.perf_counter() - t0
    assert not any(got)
    assert [metrics._excepted(replace(o, refs=o.refs[:1]), c) for o in offs[995:1005]] == [
        k < 1000 for k in range(995, 1005)
    ]
    assert elapsed < 0.5, elapsed


def test_m14_paste_exempt_fiducials_and_test_points() -> None:
    """KiCad TestPoint_Pad_* and fiducial pads have no paste by design."""
    for ref, lib in (("TP1", "Lib:FP"), ("X7", "TestPoint:TestPoint_Pad_D1.0mm"), ("FID1", "")):
        p = replace(smd(ref, (60, 20), "F.Cu", "F.Mask"), lib_id=lib or "Lib:FP")
        m = _add(_m14_base(), parts=(p,))
        assert classify(m).part_class[ref] in ("test_point", "fiducial")
        m14 = get(run(m, preset=JLC4), "M14")
        assert not [o for o in m14.offenders if o.what.startswith("paste_missing")], ref


def _qfn(ref: str, at: tuple[float, float], *, sub_pads: bool) -> Part:
    """A QFN-like part: two pasted signal pads plus a 3 x 3 mm exposed pad on F.Cu + F.Mask
    whose paste comes (optionally) from four F.Paste-only sub-pads, as KiCad's
    Package_DFN_QFN footprints do."""
    x, y = at
    pads = [
        with_layers(pad(ref, "1", "S1", (x - 2.0, y)), *GOOD_PAD),
        with_layers(pad(ref, "2", "S2", (x + 2.0, y)), *GOOD_PAD),
        with_layers(pad(ref, "3", "GND", (x, y), w=3.0, h=3.0), "F.Cu", "F.Mask"),
    ]
    if sub_pads:
        pads += [
            with_layers(pad(ref, "", "", (x + dx, y + dy), w=1.2, h=1.2), "F.Paste")
            for dx in (-0.75, 0.75)
            for dy in (-0.75, 0.75)
        ]
    return part(ref, x, y, half=3.0, pads=tuple(pads), texts=(), lib_id="Package_DFN_QFN:QFN")


def _paste_missing(m: BoardModel) -> list[str]:
    m14 = get(run(m, preset=JLC4), "M14")
    return [o.what for o in m14.offenders if o.what.startswith("paste_missing")]


def test_m14_paste_from_paste_only_sub_pads() -> None:
    """A QFN exposed pad pasted through F.Paste-only sub-pads of the same part passes; the
    same pad without sub-pads (or with another part's sub-pads) fails."""
    assert _paste_missing(_add(_m14_base(), parts=(_qfn("U7", (60, 20), sub_pads=True),))) == []
    bare = _paste_missing(_add(_m14_base(), parts=(_qfn("U7", (60, 20), sub_pads=False),)))
    assert bare == ["paste_missing: U7.3 has no F.Paste"]
    # sub-pads of another part over U7's EP do not count
    donor = _qfn("U8", (60, 20), sub_pads=True)
    donor = replace(donor, pads=tuple(q for q in donor.pads if q.number == ""))
    other = _add(_m14_base(), parts=(_qfn("U7", (60, 20), sub_pads=False), donor))
    assert _paste_missing(other) == ["paste_missing: U7.3 has no F.Paste"]


def test_m14_solder_jumper_exempt_from_paste() -> None:
    """KiCad SolderJumper pads are paste-free by design: a JPn part or a Jumper-library part
    with no pasted pad is exempt; a resistor missing paste still fails."""
    for ref, lib in (("JP1", "Lib:FP"), ("X3", "jumper:SolderJumper-2_P1.3mm_Open_Pad1.0x1.5mm")):
        jp = replace(
            part(ref, 60, 20, texts=()),
            lib_id=lib,
            pads=(
                with_layers(pad(ref, "1", "A", (59.5, 20)), "F.Cu", "F.Mask"),
                with_layers(pad(ref, "2", "B", (60.5, 20)), "F.Cu", "F.Mask"),
            ),
        )
        assert _paste_missing(_add(_m14_base(), parts=(jp,))) == [], ref
    # a jumper with one pasted pad is a normal part again
    half = replace(
        part("JP2", 60, 20, texts=()),
        pads=(
            with_layers(pad("JP2", "1", "A", (59.5, 20)), *GOOD_PAD),
            with_layers(pad("JP2", "2", "B", (60.5, 20)), "F.Cu", "F.Mask"),
        ),
    )
    assert _paste_missing(_add(_m14_base(), parts=(half,))) == [
        "paste_missing: JP2.2 has no F.Paste"
    ]
    r = replace(smd("R9", (60, 20), "F.Cu", "F.Mask"), lib_id="Resistor_SMD:R_0603")
    assert _paste_missing(_add(_m14_base(), parts=(r,))) == ["paste_missing: R9.1 has no F.Paste"]


def test_m14_na_without_preset() -> None:
    m14 = get(run(_m14_base()), "M14")
    assert m14.verdict == "n/a"
    assert m14.value is None


# ---- report shape --------------------------------------------------------------------------


def test_offender_cap_20() -> None:
    tracks = tuple(
        track(f"S{i}", "F.Cu", (10, 2 + 3 * i), (20, 2 + 3 * i), 0.15, kiid=f"t{i}")
        for i in range(25)
    )
    m = board(tracks=tracks, net_classes=(net_class("Default", 0.2),))
    m12 = get(run(m, only=frozenset({"M12"})), "M12")
    assert m12.value == 25
    assert len(m12.offenders) == 20
    assert m12.omitted == 5


def test_offenders_sorted_by_value_then_text() -> None:
    tracks = (
        track("SB", "F.Cu", (10, 10), (20, 10), 0.15, kiid="a"),
        track("SA", "F.Cu", (10, 20), (20, 20), 0.15, kiid="b"),
        track("SC", "F.Cu", (10, 30), (20, 30), 0.1, kiid="c"),
    )
    m = board(tracks=tracks, net_classes=(net_class("Default", 0.2),))
    m12 = get(run(m, only=frozenset({"M12"})), "M12")
    assert [o.nets for o in m12.offenders] == [("SC",), ("SA",), ("SB",)]


def test_only_subset() -> None:
    m = board(parts=(part("R1", 50, 40), part("R2", 1.0, 40)))
    rep = run(m, only=frozenset({"M1", "M4"}))
    assert [x.id for x in rep.metrics] == ["M1", "M4"]
    assert rep.hard_failures == 1
    assert rep.reviews == 0
    full = run(m)
    assert [x.id for x in full.metrics] == [f"M{i}" for i in range(1, 15)]
    assert full.hard_failures == sum(1 for x in full.metrics if x.verdict == "fail")
    assert full.reviews == sum(1 for x in full.metrics if x.verdict == "review")


def test_threshold_texts() -> None:
    rep = run(board())
    texts = {x.id: (x.hard, x.review) for x in rep.metrics}
    assert texts["M5"][0] == "ratio > 2.0 and extra > 5 mm"
    assert texts["M6"][0] is None
    assert texts["M1"] == ("> 0", None)


def test_hard_thresholds_verbatim() -> None:
    assert HARD == {"M4_angle_tol_deg": 0.5, "M5_ratio": 2.0, "M5_extra_mm": 5.0, "M8_mm": 5.0}
    assert REVIEW == {
        "M5_median": 1.15,
        "M5_any": 1.5,
        "M5_critical": 1.25,
        "M6_diag_frac": 0.5,
        "M7_bends_per_10mm": 1.0,
        "M8_mm": 3.0,
        "M9_in_cap_mm": 3.0,
        "M9_inductor_mm": 3.0,
        "M9_sw_copper_mm": 5.0,
        "M10_ratio": 3.0,
        "M11_fill": 0.85,
        "M12_mismatch_mm": 1.0,
    }
    assert metrics.M1_EDGE_OVERHANG is True


def test_m14_vertical_text_box() -> None:
    """P7: a 90° text's box is vertical (TextItem.angle): beside the pad it passes, the
    same text horizontal overlaps the pad."""
    flat = replace(silk((43.3, 40), text="ABCDEF"), angle=0.0)
    m14 = get(run(_add(_m14_base(), texts=(flat,)), preset=JLC4), "M14")
    assert any(o.what.startswith("silk_over_pad") for o in m14.offenders)
    up = replace(flat, angle=90.0)
    m14 = get(run(_add(_m14_base(), texts=(up,)), preset=JLC4), "M14")
    assert not [o for o in m14.offenders if o.what.startswith("silk_over_pad")]


def test_m3_accepts_warning_on_a_pad_or_field_kiid() -> None:
    """P7 (P3b residual): a warning naming a pad or a field KIID maps to its part."""
    m = with_part(board(), "R1", item_kiids=(("pad-kiid", "pad:1"), ("ref-kiid", "reference")))
    doc = BoardDoc(accepted=(AcceptedWarning("silk_edge_clearance", ("R1",), "edge", "owner"),))
    for kiid in ("pad-kiid", "ref-kiid"):
        drc = DrcResult(
            markers=(marker("silk_edge_clearance", "warning", kiid),), unconnected=0, seconds=0.1
        )
        m3 = get(run(m, drc=drc, doc=doc), "M3")
        assert m3.value == 0 and m3.verdict == "review", m3.offenders
