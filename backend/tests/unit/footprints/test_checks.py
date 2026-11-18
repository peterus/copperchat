"""fp_check rules (FP-C4, spec §8) on synthetic geometry built in code."""

from __future__ import annotations

import dataclasses
import itertools
import math
import time
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from copperchat.kicad_tools.footprints import checks, geom
from copperchat.kicad_tools.footprints.checks import (
    MAX_PAIR_SHAPES,
    WORK_BUDGET,
    CheckInput,
    CheckReport,
    Expected,
    ExpectedPad,
    Finding,
    pitch_pad_pair,
    run_checks,
    to_result,
)
from copperchat.kicad_tools.footprints.model import (
    APPROVAL_FIELD,
    REVIEW_FIELD,
    SOURCE_FIELD,
    Drill,
    FootprintGeometry,
    Pad,
    Shape,
    Text,
    geometry_hash,
)

SMD = ("F.Cu", "F.Mask", "F.Paste")
# wall-clock asserts are only a loose hang guard (slow CI); the bound itself is asserted
# on the work units a run spent (`CheckReport.work`)
HANG_S = 10.0
THT = ("*.Cu", "F.Mask", "B.Mask")
SOURCE = {SOURCE_FIELD: "datasheet rev B p. 3", REVIEW_FIELD: "yes"}


# --- builders -----------------------------------------------------------------------


def P(
    number: str,
    at: tuple[float, float],
    size: tuple[float, float] = (1.3, 3.5),
    *,
    type: str = "smd",
    layers: tuple[str, ...] = SMD,
    drill: float | None = None,
    shape: str = "rect",
    rot: float = 0.0,
) -> Pad:
    d = Drill((drill, drill), "circle") if drill else None
    return Pad(number, type, shape, at, rot, size, None, d, layers)


def rect(layer: str, x0: float, y0: float, x1: float, y1: float, w: float = 0.05) -> Shape:
    return Shape("rect", layer, ((x0, y0), (x1, y1)), None, None, w, True)


def seg(layer: str, a: tuple[float, float], b: tuple[float, float], w: float = 0.12) -> Shape:
    return Shape("segment", layer, (a, b), None, None, w, False)


def text(
    kind: str, layer: str, at: tuple[float, float], value: str, size: tuple[float, float] = (1, 1)
) -> Text:
    return Text(kind, layer, at, size, 0.0, value)


def geo(
    pads: Sequence[Pad],
    shapes: Sequence[Shape] = (),
    texts: Sequence[Text] = (),
    *,
    fields: dict[str, str] | None = None,
    mounting: str = "smd",
    scope: str = "local",
) -> FootprintGeometry:
    return FootprintGeometry(
        lib_id="CopperChat:Test",
        scope=scope,  # type: ignore[arg-type]
        description="",
        keywords="",
        mounting=mounting,  # type: ignore[arg-type]
        exclude_from_bom=False,
        exclude_from_pos=False,
        dnp=False,
        fields=dict(SOURCE if fields is None else fields),
        pads=tuple(pads),
        shapes=tuple(shapes),
        texts=tuple(texts),
        models=(),
        overrides=(),
    )


L201_PADS = (P("1", (-1.1, 0.0)), P("2", (1.1, 0.0)))
L201_CRTYD = rect("F.CrtYd", -2, -2, 2, 2)
L201_FAB = rect("F.Fab", -1.5, -1.5, 1.5, 1.5, 0.1)
L201_SILK = (seg("F.SilkS", (-0.2, -1.9), (0.2, -1.9)), seg("F.SilkS", (-0.2, 1.9), (0.2, 1.9)))
L201_TEXTS = (
    text("reference", "F.SilkS", (0, -3), "REF**"),
    text("value", "F.Fab", (0, 3), "L201"),
)


def l201(
    pads: Sequence[Pad] = L201_PADS,
    *,
    crtyd: Sequence[Shape] = (L201_CRTYD,),
    fab: Sequence[Shape] = (L201_FAB,),
    silk: Sequence[Shape] = L201_SILK,
    texts: Sequence[Text] = L201_TEXTS,
    **kw: Any,
) -> FootprintGeometry:
    return geo(pads, [*crtyd, *fab, *silk], texts, **kw)


def auto(
    pads: Sequence[Pad], *, marker: bool = True, margin: float = 0.5, **kw: Any
) -> FootprintGeometry:
    """Courtyard = pad bbox + margin, fab = pad bbox, a silk mark top-left of pad 1."""
    pts = [p for pad in pads for p in geom.pad_polygon(pad)]
    x0, y0 = min(p[0] for p in pts), min(p[1] for p in pts)
    x1, y1 = max(p[0] for p in pts), max(p[1] for p in pts)
    shapes = [
        rect("F.CrtYd", x0 - margin, y0 - margin, x1 + margin, y1 + margin),
        rect("F.Fab", x0, y0, x1, y1, 0.1),
    ]
    if marker:
        p1 = geom.pad_polygon(pads[0])
        px, py = min(p[0] for p in p1), min(p[1] for p in p1)
        shapes.append(seg("F.SilkS", (px - 0.3, py - 0.3), (px - 0.3, py - 0.1)))
    return geo(pads, shapes, **kw)


def grid(cols: int, rows: int, pitch: float, size: float) -> list[Pad]:
    return [
        P(str(r * cols + c + 1), (c * pitch, r * pitch), (size, size))
        for r in range(rows)
        for c in range(cols)
    ]


def check(g: FootprintGeometry, **kw: Any) -> CheckReport:
    kw.setdefault("local", True)
    return run_checks(CheckInput(geometry=g, **kw))


def of(report: CheckReport, rule: str) -> list[Finding]:
    return [f for f in report.findings if f.rule == rule]


# --- per-rule table -----------------------------------------------------------------


def _bottom_pad(layers: tuple[str, ...]) -> FootprintGeometry:
    pads = (P("1", (-1.1, 0.0), layers=layers), P("2", (1.1, 0.0), layers=("B.Cu", "B.Mask")))
    return l201(
        pads,
        crtyd=(rect("B.CrtYd", -2, -2, 2, 2),),
        fab=(rect("B.Fab", -1.5, -1.5, 1.5, 1.5, 0.1),),
        silk=(),
    )


def _small(margin: float) -> FootprintGeometry:
    pads = (P("1", (-0.48, 0.0), (0.5, 0.5)), P("2", (0.48, 0.0), (0.5, 0.5)))
    return geo(
        pads,
        [
            rect("F.CrtYd", -0.73 - margin, -0.25 - margin, 0.73 + margin, 0.25 + margin),
            rect("F.Fab", -0.5, -0.25, 0.5, 0.25, 0.02),
        ],
    )


TABLE: list[tuple[str, Callable[[], CheckInput], str, str, tuple[str, ...], Any, Any]] = [
    # (id, input, rule, severity, pads, measured, expected); measured/expected None = skip
    ("pin_without_pad", lambda: CheckInput(l201(), pins=["1", "2", "3"]),
     "pin_without_pad", "error", ("3",), None, None),
    ("pad_without_pin", lambda: CheckInput(l201(), pins=["1"]),
     "pad_without_pin", "warning", ("2",), None, None),
    ("pad_without_pin_allowed",
     lambda: CheckInput(l201(), pins=["1"], allow_unmatched=frozenset({"2"})),
     "pad_without_pin", "info", ("2",), None, None),
    ("pad_count_summary", lambda: CheckInput(l201(), pins=["1", "2"]),
     "pad_count_summary", "info", (), {"pins": 2, "pads": 2, "unnumbered": 0}, None),
    ("duplicate_pad_number",
     lambda: CheckInput(l201((P("1", (-1.1, 0)), P("1", (1.1, 0))))),
     "duplicate_pad_number", "info", ("1",), 2, None),
    ("expected_position",
     lambda: CheckInput(l201(), expected=Expected(
         (ExpectedPad("1", (-1.1, 0.02), (1.3, 3.5)), ExpectedPad("2", (1.1, 0), (1.3, 3.5))))),
     "expected_mismatch", "error", ("1",), (-1.1, 0.0), (-1.1, 0.02)),
    ("expected_size",
     lambda: CheckInput(l201(), expected=Expected(
         (ExpectedPad("1", (-1.1, 0), (1.3, 3.6)), ExpectedPad("2", (1.1, 0), (1.3, 3.5))))),
     "expected_mismatch", "error", ("1",), (1.3, 3.5), (1.3, 3.6)),
    ("expected_drill",
     lambda: CheckInput(l201(), expected=Expected(
         (ExpectedPad("1", (-1.1, 0), (1.3, 3.5), 0.5), ExpectedPad("2", (1.1, 0), (1.3, 3.5))))),
     "expected_mismatch", "error", ("1",), None, 0.5),
    ("expected_pitch", lambda: CheckInput(l201(), expected=Expected(pitch=2.3)),
     "expected_mismatch", "error", ("1", "2"), 2.2, 2.3),
    ("expected_courtyard", lambda: CheckInput(l201(), expected=Expected(courtyard=(4.1, 4.0))),
     "expected_mismatch", "error", (), (4.0, 4.0), (4.1, 4.0)),
    ("expected_body", lambda: CheckInput(l201(), expected=Expected(body=(3.0, 3.1))),
     "expected_mismatch", "error", (), (3.0, 3.0), (3.0, 3.1)),
    ("expected_extra_pad",
     lambda: CheckInput(l201(), expected=Expected((ExpectedPad("1", (-1.1, 0), (1.3, 3.5)),))),
     "expected_missing_pad", "error", ("2",), None, None),
    ("expected_missing_pad",
     lambda: CheckInput(l201(), expected=Expected((
         ExpectedPad("1", (-1.1, 0), (1.3, 3.5)), ExpectedPad("2", (1.1, 0), (1.3, 3.5)),
         ExpectedPad("3", (0, 0), (1, 1))))),
     "expected_missing_pad", "error", ("3",), None, None),
    ("courtyard_missing", lambda: CheckInput(l201(crtyd=())),
     "courtyard_missing", "error", (), None, None),
    ("courtyard_open",
     lambda: CheckInput(l201(crtyd=(
         seg("F.CrtYd", (-2, -2), (2, -2)), seg("F.CrtYd", (2, -2), (2, 2)),
         seg("F.CrtYd", (2, 2), (-2, 2))))),
     "courtyard_missing", "error", (), None, None),
    ("courtyard_not_enclosing_pad",
     lambda: CheckInput(l201((P("1", (-1.1, 0)), P("2", (1.5, 0))))),
     "courtyard_not_enclosing", "error", ("2",), None, None),
    ("courtyard_not_enclosing_fab",
     lambda: CheckInput(l201(fab=(rect("F.Fab", -2.5, -1.5, 2.5, 1.5, 0.1),))),
     "courtyard_not_enclosing", "error", (), None, None),
    ("courtyard_margin",
     lambda: CheckInput(l201(crtyd=(rect("F.CrtYd", -1.85, -1.85, 1.85, 1.85),))),
     "courtyard_margin", "warning", ("1",), 0.1, 0.25),
    ("courtyard_margin_small", lambda: CheckInput(_small(0.05)),
     "courtyard_margin", "warning", (), 0.04, 0.1),
    ("fab_missing", lambda: CheckInput(l201(fab=())),
     "fab_missing", "warning", (), None, None),
    ("silk_on_pad", lambda: CheckInput(l201(silk=(seg("F.SilkS", (-1.1, -1.9), (-1.1, 1.9)),))),
     "silk_on_pad", "warning", ("1",), 0.0, 0.1),
    ("silk_near_pad",
     lambda: CheckInput(l201(silk=(seg("F.SilkS", (-1.7, -1.86), (-0.5, -1.86)),))),
     "silk_on_pad", "warning", ("1",), 0.05, 0.1),
    ("silk_outside_courtyard",
     lambda: CheckInput(l201(silk=(seg("F.SilkS", (-0.5, -2.5), (0.5, -2.5)),))),
     "silk_outside_courtyard", "warning", (), None, None),
    ("pin1_marker_grid", lambda: CheckInput(auto(grid(2, 2, 1.5, 0.6), marker=False)),
     "pin1_marker_missing", "warning", ("1",), None, None),
    ("pin1_marker_polarized", lambda: CheckInput(l201(), polarized=True),
     "pin1_marker_missing", "warning", ("1",), None, None),
    ("mask_missing",
     lambda: CheckInput(l201((P("1", (-1.1, 0), layers=("F.Cu", "F.Paste")), P("2", (1.1, 0))))),
     "mask_missing", "error", ("1",), None, None),
    ("mask_missing_bottom", lambda: CheckInput(_bottom_pad(("B.Cu", "F.Mask", "B.Paste"))),
     "mask_missing", "error", ("1",), None, None),
    ("paste_missing",
     lambda: CheckInput(l201((P("1", (-1.1, 0), layers=("F.Cu", "F.Mask")), P("2", (1.1, 0))))),
     "paste_missing", "warning", ("1",), None, None),
    ("paste_on_tht",
     lambda: CheckInput(l201(
         (P("1", (-1.1, 0), (1.3, 1.3), type="tht", layers=(*THT, "F.Paste"), drill=0.6),
          P("2", (1.1, 0), (1.3, 1.3), type="tht", layers=THT, drill=0.6)), mounting="tht")),
     "paste_on_tht", "warning", ("1",), None, None),
    ("mounting_tht_smd_pads", lambda: CheckInput(l201(mounting="tht")),
     "mounting_mismatch", "error", (), None, None),
    ("mounting_smd_tht_pads",
     lambda: CheckInput(l201(
         (P("1", (-1.1, 0), (1.3, 1.3), type="tht", layers=THT, drill=0.6),
          P("2", (1.1, 0), (1.3, 1.3), type="tht", layers=THT, drill=0.6)), mounting="smd")),
     "mounting_mismatch", "error", (), None, None),
    ("pad_too_small",
     lambda: CheckInput(l201((P("1", (-1.1, 0), (0.1, 3.5)), P("2", (1.1, 0))))),
     "pad_too_small", "warning", ("1",), 0.1, 0.15),
    ("pad_gap_warning",
     lambda: CheckInput(l201((P("1", (-0.72, 0)), P("2", (0.72, 0))), silk=())),
     "pad_gap_small", "warning", ("1", "2"), 0.14, 0.15),
    ("pad_gap_error",
     lambda: CheckInput(l201((P("1", (-0.69, 0)), P("2", (0.69, 0))), silk=())),
     "pad_gap_small", "error", ("1", "2"), 0.08, 0.1),
    ("drill_error",
     lambda: CheckInput(l201(
         (P("1", (-1.1, 0), (1.3, 1.3), type="tht", layers=THT, drill=0.15),
          P("2", (1.1, 0), (1.3, 1.3), type="tht", layers=THT, drill=0.6)), mounting="tht")),
     "drill_small", "error", ("1",), 0.15, 0.2),
    ("drill_warning",
     lambda: CheckInput(l201(
         (P("1", (-1.1, 0), (1.3, 1.3), type="tht", layers=THT, drill=0.25),
          P("2", (1.1, 0), (1.3, 1.3), type="tht", layers=THT, drill=0.6)), mounting="tht")),
     "drill_small", "warning", ("1",), 0.25, 0.3),
    ("annular_ring_small",
     lambda: CheckInput(l201(
         (P("1", (-1.1, 0), (1.0, 1.0), type="tht", layers=THT, drill=0.8),
          P("2", (1.1, 0), (1.3, 1.3), type="tht", layers=THT, drill=0.6)), mounting="tht")),
     "annular_ring_small", "warning", ("1",), 0.1, 0.15),
    ("source_missing", lambda: CheckInput(l201(fields={REVIEW_FIELD: "yes"}), local=True),
     "source_missing", "error", (), None, None),
    ("needs_user_review", lambda: CheckInput(l201(), local=True),
     "needs_user_review", "info", (), "needs_user_review", None),
    ("approved_geometry_changed",
     lambda: CheckInput(l201(fields={
         SOURCE_FIELD: "x", REVIEW_FIELD: "no",
         APPROVAL_FIELD: "approved 2026-11-01, conversation abcd1234, geometry deadbeef"}),
         local=True),
     "approved_geometry_changed", "warning", (), "deadbeef", None),
]  # fmt: skip


@pytest.mark.parametrize(
    ("make", "rule", "severity", "pads", "measured", "expected"),
    [pytest.param(*row[1:], id=row[0]) for row in TABLE],
)
def test_rule(
    make: Callable[[], CheckInput],
    rule: str,
    severity: str,
    pads: tuple[str, ...],
    measured: Any,
    expected: Any,
) -> None:
    report = run_checks(make())
    hits = [f for f in of(report, rule) if f.severity == severity and f.pads == pads]
    assert hits, f"no {rule}/{severity} for {pads}: {report.findings}"
    f = hits[0]
    assert f.message
    if isinstance(measured, str):
        assert f.measured == measured
    elif measured is not None:
        assert f.measured == pytest.approx(measured, abs=1e-6)
    if expected is not None:
        assert f.expected == pytest.approx(expected, abs=1e-6)
    assert report.passed is (report.counts["error"] == 0)


def test_l201_target_passes() -> None:
    report = check(l201(), pins=["1", "2"])
    assert report.passed
    assert report.counts["error"] == 0
    assert report.counts["warning"] == 0, report.findings
    assert {f.rule for f in report.findings} == {"pad_count_summary", "needs_user_review"}
    assert report.geometry_hash == geometry_hash(l201())


def _j402() -> FootprintGeometry:
    pads = [P(str(i + 1), (i * 0.8, 0.0), (0.4, 1.2)) for i in range(9)]
    pads += [P("10", (-1.6, 1.5), (1.0, 1.6)), P("11", (8.0, 1.5), (1.0, 1.6))]
    return auto(pads)


def test_j402_shell_pads() -> None:
    pins = [str(i) for i in range(1, 10)]
    report = check(_j402(), pins=pins)
    assert {(f.pads, f.severity) for f in of(report, "pad_without_pin")} == {
        (("10",), "warning"),
        (("11",), "warning"),
    }
    assert report.passed, report.findings
    allowed = check(_j402(), pins=pins, allow_unmatched=frozenset({"10", "11"}))
    assert {(f.pads, f.severity) for f in of(allowed, "pad_without_pin")} == {
        (("10",), "info"),
        (("11",), "info"),
    }
    assert allowed.passed
    (summary,) = of(report, "pad_count_summary")
    assert summary.measured == {"pins": 9, "pads": 11, "unnumbered": 0}


def _drc0010j_pads() -> list[Pad]:
    ys = [-1.0, -0.5, 0.0, 0.5, 1.0]
    pads = [P(str(i + 1), (-1.45, y), (0.85, 0.3)) for i, y in enumerate(ys)]
    pads += [P(str(i + 6), (1.45, y), (0.85, 0.3)) for i, y in enumerate(reversed(ys))]
    pads.append(P("11", (0.0, 0.0), (1.2, 2.4)))
    return pads


def test_drc0010j_expected_mismatch() -> None:
    pads = _drc0010j_pads()
    exp = []
    for p in pads:
        at, size = p.at, p.size
        if p.number == "3":
            at = (at[0], at[1] + 0.05)
        if p.number == "11":
            size = (size[0] + 0.1, size[1] + 0.1)
        exp.append(ExpectedPad(p.number, at, size))
    report = check(auto(pads), expected=Expected(tuple(exp), pitch=0.5), local=False)
    mism = of(report, "expected_mismatch")
    assert {(f.pads, f.severity) for f in mism} == {(("3",), "error"), (("11",), "error")}
    assert len(mism) == 2
    assert not of(report, "expected_missing_pad")
    assert not report.passed


def test_source_missing_severity() -> None:
    (f,) = of(check(l201(fields={REVIEW_FIELD: "yes"})), "source_missing")
    assert f.severity == "error"
    (f,) = of(check(l201(fields={})), "source_missing")  # seed copy without custom fields
    assert f.severity == "error"
    assert not of(check(l201(fields={}, scope="global"), local=False), "source_missing")
    assert not of(check(l201()), "source_missing")


def test_reference_text_outside_courtyard_ok() -> None:
    report = check(l201())
    assert not of(report, "silk_outside_courtyard")
    silk = (*L201_SILK, seg("F.SilkS", (-1.0, -3.0), (1.0, -3.0)))
    (f,) = of(check(l201(silk=silk)), "silk_outside_courtyard")
    assert f.severity == "warning"
    over = (text("reference", "F.SilkS", (-1.1, 0.0), "R1"), L201_TEXTS[1])
    hits = of(check(l201(texts=over)), "silk_on_pad")
    assert [(f.pads, f.severity) for f in hits] == [(("1",), "warning")]
    # a user text on silk outside the courtyard is not excluded
    user = (*L201_TEXTS, text("user", "F.SilkS", (0, 3.0), "X"))
    assert of(check(l201(texts=user)), "silk_outside_courtyard")


def test_findings_capped() -> None:
    pads = grid(40, 25, 0.55, 0.5)  # 1,000 pads, 0.05 mm gaps
    report = check(auto(pads))
    gaps = of(report, "pad_gap_small")
    assert len(gaps) > 200 and all(f.severity == "error" for f in gaps)
    res = to_result(report)
    assert res["counts"] == report.counts
    assert res["counts"]["error"] == sum(f.severity == "error" for f in report.findings)
    assert len(res["findings"]) <= 200
    per_rule: dict[str, int] = {}
    for f in res["findings"]:
        per_rule[f["rule"]] = per_rule.get(f["rule"], 0) + 1
    assert max(per_rule.values()) <= 20
    assert res["truncated"] is True
    assert res["omitted"]["pad_gap_small"] == len(gaps) - per_rule["pad_gap_small"]
    total = sum(per_rule.values()) + sum(res["omitted"].values())
    assert total == len(report.findings)
    assert res["passed"] is False and res["geometry_hash"] == report.geometry_hash


def test_to_result_shape() -> None:
    res = to_result(check(l201(), pins=["1", "2"]))
    assert res["passed"] is True and res["truncated"] is False and "omitted" not in res
    assert res["counts"] == {"error": 0, "warning": 0, "info": 2}
    f = next(f for f in res["findings"] if f["rule"] == "pad_count_summary")
    assert set(f) >= {"rule", "severity", "message"}
    assert "at" not in f  # optional keys only when set
    gap = to_result(check(l201((P("1", (-0.69, 0)), P("2", (0.69, 0))), silk=())))
    g = next(f for f in gap["findings"] if f["rule"] == "pad_gap_small")
    assert g["pads"] == ["1", "2"] and g["measured"] == pytest.approx(0.08)


def test_pending_counts_unreviewed() -> None:
    fields = {
        SOURCE_FIELD: "x",
        REVIEW_FIELD: "no",
        APPROVAL_FIELD: f"approved 2026-11-01, conversation abcd1234, geometry {'0' * 8}",
    }
    g = l201(fields=fields)
    fields[APPROVAL_FIELD] = fields[APPROVAL_FIELD].replace("0" * 8, geometry_hash(g)[:8])
    g = l201(fields=fields)
    assert not of(check(g), "needs_user_review")
    assert not of(check(g), "approved_geometry_changed")
    (f,) = of(check(g, pending=True), "needs_user_review")
    assert f.severity == "info" and f.measured == "unreviewed"


def test_cost_bound() -> None:
    pads = grid(50, 50, 1.0, 0.5)  # 2,500 pads
    t = time.perf_counter()
    report = check(auto(pads), pins=[str(i) for i in range(1, 2501)])
    assert time.perf_counter() - t < HANG_S
    assert report.work < WORK_BUDGET // 10  # the pair rules never started
    (f,) = of(report, "too_complex_partial_check")
    assert f.severity == "info"
    assert "pad_gap_small" in f.message and "silk_on_pad" in f.message
    assert not of(report, "pad_gap_small") and not of(report, "silk_on_pad")
    assert of(report, "pad_count_summary") and of(report, "needs_user_review")
    shapes = [seg("F.SilkS", (i, 100), (i + 0.5, 100)) for i in range(5001)]
    many = dataclasses.replace(l201(), shapes=(*l201().shapes, *shapes))
    assert of(check(many), "too_complex_partial_check")
    assert not of(check(l201()), "too_complex_partial_check")


def _brute_gaps(pads: Sequence[Pad]) -> set[tuple[tuple[str, ...], str]]:
    out = set()
    for a, b in itertools.combinations(pads, 2):
        d = min(geom.distance(x, y) for x in geom.pad_parts(a) for y in geom.pad_parts(b))
        if d < 0.1 - 1e-6:
            out.add(((a.number, b.number), "error"))
        elif d < 0.15 - 1e-6:
            out.add(((a.number, b.number), "warning"))
    return out


def test_pad_gap_uses_sweep() -> None:
    pads = grid(40, 25, 0.5, 0.4)
    t = time.perf_counter()
    report = check(auto(pads))
    assert time.perf_counter() - t < HANG_S
    assert len(of(report, "pad_gap_small")) > 1000
    assert not of(report, "too_complex_partial_check") and report.work < WORK_BUDGET // 2
    sub = grid(10, 10, 0.5, 0.4)
    got = {
        (tuple(sorted(f.pads, key=int)), f.severity) for f in of(check(auto(sub)), "pad_gap_small")
    }
    want = {(tuple(sorted(p, key=int)), s) for p, s in _brute_gaps(sub)}
    assert got == want and len(want) > 100


def test_negative_cases() -> None:
    assert not of(check(_small(0.15)), "courtyard_margin")
    assert not of(check(_bottom_pad(("B.Cu", "B.Mask", "B.Paste"))), "mask_missing")
    assert not of(check(_bottom_pad(("B.Cu", "B.Mask", "B.Paste"))), "courtyard_missing")
    assert not of(check(auto(grid(2, 2, 1.5, 0.6))), "pin1_marker_missing")
    # same-number pads (EP split) never give a gap finding
    assert not of(check(l201((P("1", (-0.69, 0)), P("1", (0.69, 0))), silk=())), "pad_gap_small")
    # pads on opposite sides do not share copper
    pads = (P("1", (-0.69, 0)), P("2", (0.69, 0), layers=("B.Cu", "B.Mask", "B.Paste")))
    assert not of(check(l201(pads, silk=())), "pad_gap_small")


def _soic8(fab: Sequence[tuple[float, float]]) -> FootprintGeometry:
    """SOIC-8-like: the F.Fab body (one closed polygon) overlaps every pad row."""
    ys = (-1.905, -0.635, 0.635, 1.905)
    pads = [P(str(i + 1), (-2.475, y), (1.95, 0.6)) for i, y in enumerate(ys)]
    pads += [P(str(8 - i), (2.475, y), (1.95, 0.6)) for i, y in enumerate(ys)]
    body = Shape("polygon", "F.Fab", tuple(fab), None, None, 0.1, True)
    return geo(pads, [rect("F.CrtYd", -3.7, -2.7, 3.7, 2.7), body])


def test_pin1_chamfer_in_fab_polygon() -> None:
    chamfered = [(-0.95, -2.45), (1.95, -2.45), (1.95, 2.45), (-1.95, 2.45), (-1.95, -1.45)]
    assert not of(check(_soic8(chamfered)), "pin1_marker_missing")
    plain = [(-1.95, -2.45), (1.95, -2.45), (1.95, 2.45), (-1.95, 2.45)]
    assert of(check(_soic8(plain)), "pin1_marker_missing")
    # the chamfer at the pad-4 corner is nearer to pad 4 than to pad 1: no mark
    wrong = [(-1.95, -2.45), (1.95, -2.45), (1.95, 2.45), (-0.95, 2.45), (-1.95, 1.45)]
    assert of(check(_soic8(wrong)), "pin1_marker_missing")


def test_pin1_axis_aligned_outline_is_no_mark() -> None:
    """A polarized 2-pad part whose closed fab outline (rect or 4-corner polygon) has
    only axis-aligned edges: no single edge counts, the warning stays."""
    corners = ((-1.5, -1.5), (1.5, -1.5), (1.5, 1.5), (-1.5, 1.5))
    poly = Shape("polygon", "F.Fab", corners, None, None, 0.1, True)
    for fab in (L201_FAB, poly):
        assert of(check(l201(fab=(fab,), silk=()), polarized=True), "pin1_marker_missing")


def test_pitch_pad_pair() -> None:
    pair = pitch_pad_pair(geo(_drc0010j_pads()))
    assert pair is not None and (pair[0].number, pair[1].number) == ("1", "2")
    pads = [P("10", (0, 0)), P("2", (5, 0)), P("1", (1, 0))]
    pair = pitch_pad_pair(geo(pads))
    assert pair is not None and pair[1].number == "2"
    assert pitch_pad_pair(geo([P("1", (0, 0))])) is None


# --- review-loop round 1 ----------------------------------------------------------------


def _custom(number: str, at: tuple[float, float], prims: Sequence[dict[str, Any]]) -> Pad:
    extra = {"anchor": "rect", "primitives": tuple(prims)}
    return Pad(
        number, "smd", "custom", at, 0.0, (0.2, 0.2), None, None, SMD, tuple(sorted(extra.items()))
    )


def test_custom_pad_stroke_crossing_courtyard_is_not_enclosed() -> None:
    """A primitive segment whose centre line ends 0.1 mm inside the courtyard but whose
    0.4 mm stroke crosses it is outside, not only a margin warning."""
    stroke = {"kind": "segment", "points": [(0.0, 0.0), (0.0, 1.9)], "width": 0.4}
    pads = (_custom("1", (-1.1, 0.0), [stroke]), P("2", (1.1, 0.0), (1.3, 1.0)))
    g = geo(pads, [L201_CRTYD, L201_FAB, *L201_SILK], L201_TEXTS)
    (f,) = of(check(g), "courtyard_not_enclosing")
    assert f.severity == "error" and f.pads == ("1",)
    thin = {**stroke, "width": 0.1}
    g = geo((_custom("1", (-1.1, 0.0), [thin]), pads[1]), [L201_CRTYD, L201_FAB, *L201_SILK])
    assert not of(check(g), "courtyard_not_enclosing")


def test_diagonal_stroke_extremes_checked() -> None:
    """A diagonal stroked segment: the stadium's axis extremes around its ends count."""
    stroke = {"kind": "segment", "points": [(0.0, 0.0), (0.8, 0.8)], "width": 0.6}
    # centre line ends at (0.9, 1.8): inside the +-2 courtyard; the cap reaches y = 2.1
    pads = (_custom("1", (0.1, 1.0), [stroke]), P("2", (-1.1, 0.0), (0.5, 0.5)))
    g = geo(pads, [L201_CRTYD])
    assert [f.pads for f in of(check(g), "courtyard_not_enclosing")] == [("1",)]


def _many_segments(n: int) -> list[dict[str, Any]]:
    """A zig-zag of n short stroked segments, about 0.5 x 0.5 mm."""
    pts = [(0.5 * (i % 2), 0.5 * i / n) for i in range(n + 1)]
    return [{"kind": "segment", "points": [pts[i], pts[i + 1]], "width": 0.05} for i in range(n)]


def test_pad_with_many_parts_is_reported_and_fast() -> None:
    big = _many_segments(3000)
    pads = (_custom("1", (-0.3, 0.0), big), _custom("2", (0.3, 0.0), big))
    g = geo(pads, [rect("F.CrtYd", -2, -2, 2, 2), rect("F.SilkS", -0.5, -0.5, 1.0, 1.0)])
    t0 = time.perf_counter()
    report = check(g)
    assert time.perf_counter() - t0 < HANG_S
    assert report.work <= WORK_BUDGET
    (f,) = of(report, "too_complex_partial_check")
    assert f.pads == ("1", "2") and f.expected == {"pieces": 256, "vertices": 512}
    assert not of(report, "pad_gap_small") and not of(report, "silk_on_pad")


def test_pads_below_part_cap_still_compared_quickly() -> None:
    """250 + 250 pieces (below the cap) overlap: the lazy pair search still finds the
    gap 0 and stops early."""
    few = _many_segments(250)
    pads = (_custom("1", (0.0, 0.0), few), _custom("2", (0.3, 0.0), few))
    g = geo(pads, [rect("F.CrtYd", -2, -2, 2, 2)])
    t0 = time.perf_counter()
    report = check(g)
    assert time.perf_counter() - t0 < HANG_S
    assert not of(report, "too_complex_partial_check")
    assert report.work < WORK_BUDGET // 10  # gap 0 found early: the rest not charged
    (f,) = of(report, "pad_gap_small")
    assert f.measured == 0.0 and f.severity == "error"


# --- review-loop round 2 ----------------------------------------------------------------


def _ngon(number: str, at: tuple[float, float], n: int, r: float = 0.5) -> Pad:
    """A custom pad whose single primitive is an n-vertex polygon (radius r)."""
    pts = [(r * math.cos(2 * math.pi * k / n), r * math.sin(2 * math.pi * k / n)) for k in range(n)]
    return _custom(number, at, [{"kind": "polygon", "points": pts, "width": 0.0}])


def test_pads_with_many_vertices_are_reported_and_fast() -> None:
    """Two adjacent 2000-vertex polygon pads: excluded from the pair rules (O(Va * Vb))."""
    pads = (_ngon("1", (-0.55, 0.0), 2000), _ngon("2", (0.55, 0.0), 2000))
    g = geo(pads, [rect("F.CrtYd", -2, -2, 2, 2)])
    t0 = time.perf_counter()
    report = check(g)
    assert time.perf_counter() - t0 < HANG_S
    assert report.work < WORK_BUDGET // 10  # left out of the pair rules up front
    (f,) = of(report, "too_complex_partial_check")
    assert f.pads == ("1", "2")
    assert f.measured == {
        "skipped": {"complex_pads": ["pad_gap_small", "silk_on_pad"]},
        "pieces": 2,
        "vertices": 2004,  # + anchor
    }
    assert "pin1" not in f.message  # 2 unpolarized pads: the pin-1 rule does not run
    assert not of(report, "pad_gap_small")


def test_pair_work_budget_bounds_total() -> None:
    """Many 400-vertex pads (below the cap), all touching their neighbours: the
    footprint-level budget stops the pair rules and says so."""
    pads = [_ngon(str(k + 1), (k * 0.9, 0.0), 400) for k in range(20)]
    g = geo(pads, [rect("F.CrtYd", -1, -1, 19, 1)])
    t0 = time.perf_counter()
    report = check(g)
    assert time.perf_counter() - t0 < HANG_S
    (f,) = of(report, "too_complex_partial_check")
    assert f.measured["skipped"] == {"budget": ["pad_gap_small"]} and not f.pads
    assert f.measured["work"] == report.work <= WORK_BUDGET
    assert f.expected == {"work": WORK_BUDGET}
    assert of(report, "pad_gap_small")  # the pairs within the budget are still measured


def test_pin1_bounded_with_complex_pads() -> None:
    """3 pads of 3000 segments plus 300 silk rects next to pads 2 and 3 (out of pin-1
    reach or nearer to another pad): pin 1 measures complex pads by their bounding box,
    so it stays fast; the warning is there without a mark and gone with only the mark."""
    big = _many_segments(3000)
    pads = [_custom(str(k + 1), (k * 2.0, 0.0), big) for k in range(3)]
    crtyd = rect("F.CrtYd", -1, -1, 32, 2)
    silk = [rect("F.SilkS", 1.6 + 0.1 * k, 1.0, 1.65 + 0.1 * k, 1.05) for k in range(300)]
    mark = seg("F.SilkS", (-0.4, -0.4), (-0.4, -0.1))
    t0 = time.perf_counter()
    report = check(geo(pads, [crtyd, *silk]))
    assert time.perf_counter() - t0 < HANG_S
    assert report.work < WORK_BUDGET // 2  # complex pads cost a 4-vertex box each
    assert of(report, "pin1_marker_missing")
    (f,) = of(report, "too_complex_partial_check")
    assert f.measured["skipped"]["bounding_box"] == ["pin1_marker_missing"]
    report = check(geo(pads, [crtyd, mark]))
    assert not of(report, "pin1_marker_missing")
    report = check(geo(pads, [crtyd, *silk, mark]))
    assert not of(report, "pin1_marker_missing")


# --- review-loop round 3: one shared work budget ----------------------------------------


def _skipped(report: CheckReport) -> dict[str, list[str]]:
    (f,) = of(report, "too_complex_partial_check")
    assert f.severity == "info"
    for rules in f.measured["skipped"].values():
        assert all(r in f.message for r in rules)
    skipped: dict[str, list[str]] = f.measured["skipped"]
    return skipped


def _timed(g: FootprintGeometry, **kw: Any) -> CheckReport:
    """`check` with the bound asserted on the work spent; the clock only guards hangs."""
    t0 = time.perf_counter()
    report = check(g, **kw)
    assert time.perf_counter() - t0 < HANG_S
    assert report.work <= WORK_BUDGET
    return report


def test_courtyard_containment_is_budgeted() -> None:
    """2000 pads inside a 10k-vertex courtyard ring: the band index makes each point
    test a few edges, all within budget; a comb of 4000 full-height teeth (every edge
    in every band) is refused before its index is built."""
    pads = grid(50, 40, 1.0, 0.5)
    n = 10_000
    ring = tuple(
        (24.5 + 40 * math.cos(2 * math.pi * k / n), 19.5 + 40 * math.sin(2 * math.pi * k / n))
        for k in range(n)
    )
    crtyd = Shape("polygon", "F.CrtYd", ring, None, None, 0.05, False)
    report = _timed(geo(pads, [crtyd, rect("F.Fab", 0, 0, 49, 39, 0.1)]))
    assert not of(report, "too_complex_partial_check") and not of(report, "courtyard_missing")
    assert not of(report, "courtyard_not_enclosing")
    comb: list[tuple[float, float]] = []
    for k in range(4000):
        x = -1 + 51 * k / 4000
        comb += [(x, -1.0), (x, 40.0), (x + 0.005, 40.0), (x + 0.005, -1.0)]
    crtyd = Shape("polygon", "F.CrtYd", tuple(comb), None, None, 0.05, False)
    report = _timed(geo(pads, [crtyd, rect("F.Fab", 0, 0, 49, 39, 0.1)]))
    skipped = _skipped(report)
    assert skipped["budget"][:2] == ["courtyard_not_enclosing", "courtyard_margin"]
    assert not of(report, "courtyard_missing") and not of(report, "courtyard_not_enclosing")


def test_pin1_silk_side_is_budgeted() -> None:
    """A 20k-vertex silk ring around a 500-vertex pad 1 (just below the complex-pad
    cap): every ring edge is as near as the best so far, so no box test prunes it and
    each edge-to-pad distance is charged."""
    n = 20_000
    pts = tuple(
        (0.6 * math.cos(2 * math.pi * k / n), 0.6 * math.sin(2 * math.pi * k / n)) for k in range(n)
    )
    poly = Shape("polygon", "F.SilkS", pts, None, None, 0.12, False)
    pads = [_ngon("1", (0, 0), 500, 0.25), P("2", (3, 0), (0.5, 0.5)), P("3", (6, 0), (0.5, 0.5))]
    report = _timed(geo(pads, [rect("F.CrtYd", -2, -2, 7, 2), poly]))
    assert "pin1_marker_missing" in _skipped(report)["budget"]
    assert not of(report, "pin1_marker_missing")  # no verdict without the work


def test_pin1_diagonal_edges_times_pads_is_budgeted() -> None:
    """20k diagonal edges within reach of pad 1, as near to pad 2, and 5000 more pads:
    each edge's mark test scans every other pad."""
    n = 20_000
    pts = tuple((-0.75 + 0.0001 * (k % 2), -1 + 0.0001 * k) for k in range(n))
    poly = Shape("polygon", "F.SilkS", pts, None, None, 0.0, False)
    # pad 2's right edge (-1.2499) is as far from the zig-zag as pad 1's left (-0.25)
    pads = [P("1", (0, 0), (0.5, 0.5)), P("2", (-1.4999, 0), (0.5, 0.5))]
    pads += [
        P(str(k + 3), (3 + (k % 100) * 0.6, (k // 100) * 0.6), (0.3, 0.3)) for k in range(5000)
    ]
    report = _timed(geo(pads, [rect("F.CrtYd", -2, -2, 63, 31), poly]))
    skipped = _skipped(report)
    assert "pin1_marker_missing" in skipped["budget"]
    assert skipped["too_many_items"] == ["pad_gap_small", "silk_on_pad"]
    assert not of(report, "pin1_marker_missing")


def test_silk_texts_count_toward_shape_cap() -> None:
    """2000 pads plus 20k silk texts: the texts count as shapes for the pair rules."""
    pads = grid(50, 40, 1.0, 0.5)
    texts = [text("user", "F.SilkS", (k % 50, (k // 50) % 40), "X") for k in range(20_000)]
    report = _timed(geo(pads, [rect("F.CrtYd", -1, -1, 50, 40)], texts))
    f = of(report, "too_complex_partial_check")[0]
    assert _skipped(report)["too_many_items"] == ["pad_gap_small", "silk_on_pad"]
    assert f.measured["shapes"] == 20_001 and f.expected["shapes"] == MAX_PAIR_SHAPES


def test_silk_texts_below_cap_are_swept_against_copper_only() -> None:
    """2000 pads plus 4900 stacked silk texts on them: the copper x silk sweep never
    enumerates text pairs, and the distances it measures are charged to the budget."""
    pads = grid(50, 40, 1.0, 0.5)
    texts = [text("user", "F.SilkS", (0.0, 0.0), "WIDE-TEXT" * 20) for _ in range(4900)]
    report = _timed(geo(pads, [rect("F.CrtYd", -200, -200, 200, 200)], texts))
    assert "silk_on_pad" in _skipped(report)["budget"]
    assert of(report, "silk_on_pad")  # the pairs within the budget still count


def test_partial_info_when_pair_rules_skip_early() -> None:
    """Too many pads (pair rules return early) and a complex pad 1 on a polarized part:
    one info names the pair rules and that pin 1 used the bounding box."""
    big = _many_segments(3000)
    pads = [_custom("1", (0.0, 0.0), big)]
    pads += [P(str(k + 2), (2 + (k % 50) * 1.0, (k // 50) * 1.0), (0.5, 0.5)) for k in range(2000)]
    mark = seg("F.SilkS", (-0.4, -0.4), (-0.4, -0.1))
    report = _timed(geo(pads, [rect("F.CrtYd", -1, -1, 52, 41), mark]))
    assert not of(report, "pin1_marker_missing")
    (f,) = of(report, "too_complex_partial_check")
    assert f.measured["skipped"] == {
        "too_many_items": ["pad_gap_small", "silk_on_pad"],
        "bounding_box": ["pin1_marker_missing"],
    }
    assert f.pads == ("1",) and "pin1_marker_missing" in f.message


def test_closed_outline_building_is_budgeted() -> None:
    """20k courtyard segments with all their ends in one point: every endpoint lookup
    scans the same grid cell; no courtyard verdict without the work."""
    segs = [seg("F.CrtYd", (0, 0), (0, 0)) for _ in range(20_000)]
    report = _timed(geo(L201_PADS, segs))
    assert _skipped(report)["budget"][:2] == ["courtyard_missing", "courtyard_not_enclosing"]
    assert not of(report, "courtyard_missing")


GUARD: list[tuple[str, Callable[[], CheckReport], list[tuple[str, str, tuple[str, ...], str]]]] = [
    ("l201", lambda: check(l201(), pins=["1", "2"]), [
        ("pad_count_summary", "info", (), "2 pins / 2 pads / 0 unnumbered"),
        ("needs_user_review", "info", (), "not approved by the user yet (needs_user_review)"),
    ]),
    ("l201_polarized", lambda: check(l201(), polarized=True), [
        ("pin1_marker_missing", "warning", ("1",),
         "no silk or fab mark next to pad 1 (within 4.500 mm)"),
        ("needs_user_review", "info", (), "not approved by the user yet (needs_user_review)"),
    ]),
    ("j402", lambda: check(_j402(), pins=[str(i) for i in range(1, 10)]), [
        ("pad_without_pin", "warning", ("10",), "pad 10 has no symbol pin"),
        ("pad_without_pin", "warning", ("11",), "pad 11 has no symbol pin"),
        ("pad_count_summary", "info", (), "9 pins / 11 pads / 0 unnumbered"),
        ("needs_user_review", "info", (), "not approved by the user yet (needs_user_review)"),
    ]),
    ("drc0010j", lambda: check(auto(_drc0010j_pads())), [
        ("needs_user_review", "info", (), "not approved by the user yet (needs_user_review)"),
    ]),
]  # fmt: skip


@pytest.mark.parametrize(("name", "run", "want"), GUARD, ids=[g[0] for g in GUARD])
def test_budget_leaves_normal_footprints_alone(
    name: str, run: Callable[[], CheckReport], want: list[tuple[str, str, tuple[str, ...], str]]
) -> None:
    """Normal footprints: no partial-check info, the findings exactly as before."""
    report = run()
    assert [(f.rule, f.severity, f.pads, f.message) for f in report.findings] == want


# --- review-loop round 4: cost model, fine-pitch guard, every budgeted family -----------


def _qfp(n: int, pitch: float = 0.4, width: float = 0.3, length: float = 1.475) -> list[Pad]:
    """A stock-like LQFP: n roundrect pads (ratio 0.25) on 4 sides, numbered counter-
    clockwise from the top of the left side, the rows `length / 2 + 1.5` mm out."""
    m = n // 4
    half = (m - 1) / 2 * pitch
    c = half + length / 2 + 1.325
    pads: list[Pad] = []
    for side in range(4):
        for k in range(m):
            t = -half + k * pitch
            at, rot = [((-c, t), 0.0), ((t, c), 90.0), ((c, -t), 0.0), ((-t, -c), 90.0)][side]
            num = str(side * m + k + 1)
            pads.append(Pad(num, "smd", "roundrect", at, rot, (length, width), 0.25, None, SMD))
    return pads


@pytest.mark.parametrize("n", [176, 216])
def test_fine_pitch_qfp_is_checked_in_full(n: int) -> None:
    """Guard: a 176 / 216-pad 0.4 mm-pitch roundrect LQFP is checked completely (no
    partial info) and every neighbour pair on a side is a 0.1 mm gap warning. A roundrect
    pad is a 4-vertex `Rounded` for distances, so this costs a fraction of the budget."""
    pads = _qfp(n)
    report = _timed(auto(pads))
    assert not of(report, "too_complex_partial_check")
    m = n // 4
    want = {(str(s * m + k + 1), str(s * m + k + 2)) for s in range(4) for k in range(m - 1)}
    gaps = of(report, "pad_gap_small")
    assert {f.pads for f in gaps} == want
    assert all(f.severity == "warning" and f.measured == pytest.approx(0.1) for f in gaps)
    assert report.work < WORK_BUDGET // 10
    # stock pad width (0.25 mm): gap 0.15 mm, no finding, still no partial info
    stock = _timed(auto(_qfp(n, width=0.25)))
    assert not of(stock, "too_complex_partial_check") and not of(stock, "pad_gap_small")


def test_min_dist_charges_only_measured_pairs() -> None:
    """The pair distance is charged per piece pair measured, not up front for all."""
    pads = (_custom("1", (-0.3, 0.0), _many_segments(200)), P("2", (5.0, 0.0), (0.5, 0.5)))
    report = _timed(geo(pads, [rect("F.CrtYd", -2, -2, 6, 2)]))
    assert report.work < 10_000  # pads 5 mm apart: no distance measured at all


@pytest.fixture
def small_budget(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], None]:
    def set_budget(units: int) -> None:
        monkeypatch.setattr(checks, "WORK_BUDGET", units)

    return set_budget


def test_silk_outside_is_budgeted(small_budget: Callable[[int], None]) -> None:
    """3000 silk segments inside the courtyard: each one's chords and points are
    charged, built one at a time; the rule stops when the budget is used up."""
    small_budget(5_000)
    silk = [seg("F.SilkS", (-1.9 + 0.001 * k, 1.5), (-1.9 + 0.001 * k, 1.6)) for k in range(3000)]
    report = _timed(l201(silk=silk))
    assert _skipped(report)["budget"][0] == "silk_outside_courtyard"
    assert report.work <= 5_000


def test_expected_pad_matching_is_budgeted(small_budget: Callable[[int], None]) -> None:
    """3000 expected pads: each lookup is charged; matching stops with the budget."""
    small_budget(5_000)
    exp = Expected(pads=tuple(ExpectedPad("1", (-1.1, 0.0), (1.3, 3.5)) for _ in range(3000)))
    report = _timed(l201(), expected=exp)
    assert _skipped(report)["budget"][:2] == ["expected_mismatch", "expected_missing_pad"]


def test_margin_pairs_are_budgeted(small_budget: Callable[[int], None]) -> None:
    """3000 fab segments along the courtyard edge: containment completes, then the
    margin's edge x item pairs and distances run out of budget."""
    small_budget(20_000)
    fab = [seg("F.Fab", (-1.9 + 0.001 * k, -1.9), (-1.9 + 0.001 * k, -1.85)) for k in range(3000)]
    report = _timed(l201(fab=fab, silk=()))
    skipped = _skipped(report)
    assert skipped["budget"][0] == "courtyard_margin"
    assert "courtyard_not_enclosing" not in skipped["budget"]


def test_fab_containment_is_budgeted(small_budget: Callable[[int], None]) -> None:
    """3000 fab segments in an octagon courtyard: the body's points x courtyard vertices
    are charged after the pads'; refused, neither enclosure nor margin has a verdict."""
    small_budget(20_000)
    octagon = tuple(
        (2.5 * math.cos(k * math.pi / 4), 2.5 * math.sin(k * math.pi / 4)) for k in range(8)
    )
    crtyd = Shape("polygon", "F.CrtYd", octagon, None, None, 0.05, False)
    fab = [seg("F.Fab", (-1.0 + 0.0005 * k, 0.5), (-1.0 + 0.0005 * k, 0.6)) for k in range(3000)]
    report = _timed(l201(crtyd=(crtyd,), fab=fab, silk=()))
    assert _skipped(report)["budget"][:2] == ["courtyard_not_enclosing", "courtyard_margin"]
    assert not of(report, "courtyard_not_enclosing")
    assert report.work < 12_000  # the pads' points and the chords; the fab test refused


def test_tessellation_is_charged(small_budget: Callable[[int], None]) -> None:
    """20k tiny silk arcs: their chords are charged before they are built."""
    small_budget(50_000)
    arc = Shape("arc", "F.SilkS", ((0.0, 0.0), (0.01, 0.01), (0.02, 0.0)), None, None, 0.12, False)
    report = _timed(l201(silk=[arc] * 20_000))
    assert "silk_outside_courtyard" in _skipped(report)["budget"]


def test_courtyard_points_counted_before_built(small_budget: Callable[[int], None]) -> None:
    """A custom pad of 2000 stroked arcs (32 chords x 16 points each): its containment
    points are counted and refused before any is made."""
    small_budget(100_000)
    arc = {"kind": "arc", "points": [(0.0, 0.0), (0.5, 0.5), (1.0, 0.0)], "width": 0.1}
    pads = (_custom("1", (-1.1, 0.0), [arc] * 2000), P("2", (1.1, 0.0)))
    report = _timed(geo(pads, [rect("F.CrtYd", -3, -3, 3, 3)]))
    assert _skipped(report)["budget"][:2] == ["courtyard_not_enclosing", "courtyard_margin"]
    assert report.work < 100
