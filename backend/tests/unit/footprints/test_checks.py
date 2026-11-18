"""fp_check rules (FP-C4, spec §8) on synthetic geometry built in code."""

from __future__ import annotations

import dataclasses
import itertools
import time
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from copperchat.kicad_tools.footprints import geom
from copperchat.kicad_tools.footprints.checks import (
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
    assert time.perf_counter() - t < 2.0
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
    assert time.perf_counter() - t < 2.0
    assert len(of(report, "pad_gap_small")) > 1000
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


def test_pitch_pad_pair() -> None:
    pair = pitch_pad_pair(geo(_drc0010j_pads()))
    assert pair is not None and (pair[0].number, pair[1].number) == ("1", "2")
    pads = [P("10", (0, 0)), P("2", (5, 0)), P("1", (1, 0))]
    pair = pitch_pad_pair(geo(pads))
    assert pair is not None and pair[1].number == "2"
    assert pitch_pad_pair(geo([P("1", (0, 0))])) is None
