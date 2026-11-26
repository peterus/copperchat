"""JLCPCB fab planning (SP4 P8 Task 2, `cc/board/fab.py`): kicad-cli commands, BOM/CPL rows
from kicad-cli CSV text (built here from rows, never stored), the fab report."""

from __future__ import annotations

import csv
import dataclasses
import io
from dataclasses import replace

from copperchat.board.boardmd import AcceptedWarning
from copperchat.board.fab import (
    BOM_HEADER,
    CPL_HEADER,
    FAB_TIMEOUTS_S,
    bom_rows,
    cpl_rows,
    csv_text,
    custom_rules,
    fab_commands,
    fab_report,
    min_pad_pitch,
    stencil_sides,
)
from copperchat.board.metrics import Metric, MetricsReport
from copperchat.board.model import Pad
from copperchat.board.presets import PRESETS
from tests.unit.board.builders import board, part

LAYERS_4 = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")


def _csv(header: list[str], rows: list[list[str]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def _bom(rows: list[list[str]]) -> str:
    return _csv(["Value", "Reference", "Footprint", "LCSC", "MPN"], rows)


def _pos(rows: list[list[str]]) -> str:
    return _csv(["Ref", "Val", "Package", "PosX", "PosY", "Rot", "Side"], rows)


def _cmd(cmds: tuple, sub: str):  # type: ignore[no-untyped-def]
    return next(c for c in cmds if c.args[:3] == ("pcb", "export", sub))


def _pad(ref: str, n: str, x: float, y: float, *, smd: bool = True) -> Pad:
    return Pad(
        ref=ref,
        number=n,
        net="N1",
        x=x,
        y=y,
        w=0.3,
        h=0.8,
        rot=0.0,
        shape="rect",
        layers=frozenset({"F.Cu", "F.Paste", "F.Mask"}),
        smd=smd,
        drill=None if smd else 0.8,
    )


def test_gerber_layers_4l() -> None:
    cmds = fab_commands(board(copper_layers=LAYERS_4), "src/b.kicad_pcb")
    g = _cmd(cmds, "gerbers")
    layers = g.args[g.args.index("--layers") + 1].split(",")
    assert layers[:4] == list(LAYERS_4)
    for name in ("F.Mask", "B.Mask", "F.SilkS", "B.SilkS", "F.Paste", "B.Paste", "Edge.Cuts"):
        assert name in layers
    assert "--check-zones" in g.args and "--subtract-soldermask" in g.args
    assert g.args[-1] == "src/b.kicad_pcb"
    assert g.out == "gerbers" and g.timeout_s == FAB_TIMEOUTS_S["gerbers"] == 120


def test_gerber_layers_2l() -> None:
    g = _cmd(fab_commands(board(), "b.kicad_pcb"), "gerbers")
    layers = g.args[g.args.index("--layers") + 1].split(",")
    assert [x for x in layers if x.endswith(".Cu")] == ["F.Cu", "B.Cu"]


def test_drill_separate_mm_map() -> None:
    d = _cmd(fab_commands(board(), "b.kicad_pcb"), "drill")
    a = d.args
    assert a[a.index("--format") + 1] == "excellon"
    assert a[a.index("--excellon-units") + 1] == "mm"
    assert "--excellon-separate-th" in a and "--generate-map" in a
    assert a[a.index("--map-format") + 1] == "gerberx2"
    assert d.out == "drill" and d.timeout_s == 60


def test_pos_and_bom_commands() -> None:
    cmds = fab_commands(board(), "b.kicad_pcb")
    p = _cmd(cmds, "pos")
    assert p.args[p.args.index("--format") + 1] == "csv"
    assert p.args[p.args.index("--units") + 1] == "mm"
    assert p.args[p.args.index("--side") + 1] == "both"
    assert "--exclude-dnp" in p.args and p.timeout_s == 60
    b = _cmd(cmds, "bom")
    assert b.args[b.args.index("--fields") + 1] == "Value,Reference,Footprint,LCSC,MPN"
    assert b.args[b.args.index("--group-by") + 1] == "Value,Footprint,LCSC,MPN"
    assert b.args[b.args.index("--ref-range-delimiter") + 1] == ""  # "C1,C2,C3", no "C1-C3"
    assert "--exclude-dnp" in b.args and b.timeout_s == 60
    assert [c.args[2] for c in cmds] == ["gerbers", "drill", "pos", "bom"]
    for c in cmds:  # every output stays inside the staging dir
        assert c.args[c.args.index("-o") + 1].startswith(c.out)


def test_bom_header() -> None:
    assert BOM_HEADER == ("Comment", "Designator", "Footprint", "LCSC Part #")
    text = csv_text(BOM_HEADER, bom_rows(_bom([["10k", "R1", "Lib:R_0603", "C25804", ""]])))
    assert text.splitlines()[0] == "Comment,Designator,Footprint,LCSC Part #"


def test_bom_lcsc_then_mpn() -> None:
    rows = bom_rows(
        _bom(
            [
                ["10k", "R1,R2", "Resistor_SMD:R_0603_1608Metric", "C25804", "RC0603FR-0710KL"],
                ["100n", "C1", "Capacitor_SMD:C_0402_1005Metric", "", "GRM155R71C104KA88D"],
                ["TEST", "TP1", "TestPoint_Pad_D1.0mm", "", ""],
            ]
        )
    )
    assert rows == [
        ("10k", "R1,R2", "R_0603_1608Metric", "C25804"),
        ("100n", "C1", "C_0402_1005Metric", "GRM155R71C104KA88D"),
        ("TEST", "TP1", "TestPoint_Pad_D1.0mm", ""),
    ]


def test_bom_missing_columns_tolerated() -> None:
    text = _csv(["Value", "Reference", "Footprint"], [["1k", "R9", "L:F"]])
    assert bom_rows(text) == [("1k", "R9", "F", "")]


def test_cpl_header_and_layer_names() -> None:
    assert CPL_HEADER == ("Designator", "Mid X", "Mid Y", "Layer", "Rotation")
    rows = cpl_rows(
        _pos(
            [
                ["R1", "10k", "R_0603", "141.605", "-99.695", "90.0", "top"],
                ["C9", "1u", "C_0402", "10", "20.5", "-90", "bottom"],
            ]
        )
    )
    assert rows == [
        ("R1", "141.6050mm", "-99.6950mm", "Top", "90.0"),
        ("C9", "10.0000mm", "20.5000mm", "Bottom", "-90.0"),
    ]


def test_csv_formula_injection_guard() -> None:
    evil = ['=HYPERLINK("x")', "+1", "-5V", "@SUM(A1)", "\tx", "\rx"]
    rows = bom_rows(_bom([[v, f"R{i}", f"L:{v}", v, ""] for i, v in enumerate(evil)]))
    for (comment, _, footprint, lcsc), v in zip(rows, evil, strict=True):
        assert comment == "'" + v and lcsc == "'" + v
        assert footprint.startswith("'")
    assert bom_rows(_bom([["10k", "C1", "L:C_0402", "", ""]]))[0][:2] == ("10k", "C1")
    cpl = cpl_rows(_pos([["=cmd", "v", "p", "-1.2", "3", "0", "top"]]))
    assert cpl == [("'=cmd", "-1.2000mm", "3.0000mm", "Top", "0.0")]


def test_cpl_rejects_non_numbers() -> None:
    import pytest

    with pytest.raises(ValueError):
        cpl_rows(_pos([["R1", "v", "p", "nan", "3", "0", "top"]]))
    with pytest.raises(ValueError):
        cpl_rows(_pos([["R1", "v", "p", "=1+1", "3", "0", "top"]]))
    with pytest.raises(ValueError):
        cpl_rows(_pos([["R1", "v", "p", "1", "3", "0", "middle"]]))


def test_custom_rules_with_reasons() -> None:
    # KiCad's own format (probed: pcb_set_rules writes the reason as a comment inside the rule)
    dru = (
        "(version 2)\n\n"
        '(rule "power_clear"\n'
        "\t# power nets keep a wider gap\n"
        "\t# (two lines)\n"
        "\t(condition \"A.NetClass == 'Power'\")\n"
        "\t(constraint clearance (min 0.3mm))\n"
        ")\n\n"
        "# a comment block before a rule\n"
        "# second line\n"
        '(rule "with \\"quote\\""\n'
        "\t(condition \"A.Type == 'Via'\")\n"
        ")\n"
        "(rule bare (condition \"A.Type == 'Via'\") (constraint hole_size (min 0.2mm)))\n"
        "# belongs to nothing\n"
    )
    assert custom_rules(dru) == [
        ("power_clear", "power nets keep a wider gap (two lines)"),
        ('with "quote"', "a comment block before a rule second line"),
        ("bare", ""),
    ]
    assert custom_rules("") == []


def _metrics(hard: int = 0) -> MetricsReport:
    return MetricsReport(
        metrics=(
            Metric("M1", 0, "pass", None, None, ()),
            Metric("M4", 0, "pass", None, None, ()),
            Metric("M14", 2 if hard else 0, "fail" if hard else "pass", None, None, ()),
        ),
        hard_failures=hard,
        reviews=0,
        stage="routed",
    )


def _report(model=None, **over):  # type: ignore[no-untyped-def]
    kwargs = dict(
        board_file="sp4_synth.kicad_pcb",
        board_hash="ab" * 32,
        date="2026-11-26T08:00:00Z",
        preset=PRESETS["jlcpcb-4l"],
        metrics=_metrics(),
        accepted=(AcceptedWarning("silk_overlap", ("U3",), "tiny overlap", "owner"),),
        custom_rules=(("power_clear", "power nets keep a wider gap"),),
        files=(("sp4_synth-jlcpcb.zip", 123456), ("bom-jlcpcb.csv", 512)),
    )
    kwargs.update(over)
    return fab_report(model or board(copper_layers=LAYERS_4), **kwargs)


def test_report_sections() -> None:
    text = _report()
    assert "ab" * 32 in text and "2026-11-26T08:00:00Z" in text
    assert "sp4_synth.kicad_pcb" in text
    assert "100.0 x 80.0 mm" in text  # outline size
    assert "jlcpcb-4l" in text and "4 copper layers" in text
    assert "KiCad default stackup" in text
    assert "| M1 | 0 | pass |" in text and "| M14 | 0 | pass |" in text
    assert "1.6 mm" in text and "1 oz" in text
    assert "lead-free HASL" in text
    assert "power_clear" in text and "power nets keep a wider gap" in text
    assert "silk_overlap" in text and "tiny overlap" in text
    assert "sp4_synth-jlcpcb.zip" in text and "123456" in text
    assert "not validated for JLC assembly" in text
    assert "Swept leftovers" not in text


def test_report_custom_rules_preset_none() -> None:
    text = _report(preset=None, custom_rules=())
    assert "custom rules" in text


def test_report_swept_and_stackup() -> None:
    m = board(copper_layers=LAYERS_4, stackup=(("F.Cu", "copper 0.035"), ("core", "FR4 1.065")))
    text = _report(model=m, swept=(".jlcpcb.tmp-0123456789ab",))
    assert "Swept leftovers" in text and ".jlcpcb.tmp-0123456789ab" in text
    assert "FR4 1.065" in text and "KiCad default stackup" not in text


def test_report_escapes() -> None:
    text = _report(custom_rules=(("r|1", "a | b\nc `x`"),))
    row = next(line for line in text.splitlines() if "r\\|1" in line)
    assert "a \\| b c \\`x\\`" in row
    assert row.count("|") - row.count("\\|") == 3  # still exactly two cells


def test_enig_for_fine_pitch() -> None:
    coarse = board(parts=(part("U1", pads=(_pad("U1", "1", 10, 10), _pad("U1", "2", 10.65, 10))),))
    fine = board(parts=(part("U1", pads=(_pad("U1", "1", 10, 10), _pad("U1", "2", 10.4, 10))),))
    assert min_pad_pitch(coarse) == 0.65
    assert min_pad_pitch(fine) == 0.4
    assert "ENIG" not in _report(model=coarse).split("Surface finish")[1].splitlines()[0]
    assert "ENIG" in _report(model=fine).split("Surface finish")[1].splitlines()[0]
    paste_only = dataclasses.replace(_pad("U1", "9", 10.2, 10), layers=frozenset({"F.Paste"}))
    ep = board(parts=(part("U1", pads=(*coarse.parts[0].pads, paste_only)),))
    assert min_pad_pitch(ep) == 0.65  # paste-only aperture pads are no copper pitch
    stacked = board(parts=(part("J1", pads=(_pad("J1", "A9", 5, 5), _pad("J1", "B4", 5, 5))),))
    assert min_pad_pitch(stacked) is None  # stacked pads (same position) are no pitch


def test_stencil_sides() -> None:
    top = part("U1", pads=(_pad("U1", "1", 10, 10),))
    bottom = replace(
        part("U2", side="bottom"),
        pads=(replace(_pad("U2", "1", 20, 20), layers=frozenset({"B.Cu", "B.Paste", "B.Mask"})),),
    )
    tht = replace(part("J1", side="bottom"), pads=(_pad("J1", "1", 30, 30, smd=False),))
    assert stencil_sides(board(parts=(top, tht))) == "top only"
    fiducial = replace(
        part("FID1", side="bottom"),
        pads=(replace(_pad("FID1", "1", 40, 40), layers=frozenset({"B.Cu", "B.Mask"})),),
    )
    assert stencil_sides(board(parts=(top, fiducial))) == "top only"  # no paste: no stencil
    assert stencil_sides(board(parts=(top, bottom))) == "top + bottom"
    assert "Stencil: top + bottom" in _report(model=board(parts=(top, bottom)))
