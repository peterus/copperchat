"""JLCPCB fab planning (SP4 P8, spec §7.2 "Checkpoints and output", §14): the `kicad-cli`
commands of the package, JLC BOM / CPL rows from kicad-cli's CSV text, and the Markdown fab
report. Pure (no kipy, no I/O); `cc/kicad_tools/board/fab.py` runs it.

Every BOM / CPL text cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets
a leading `'` (CSV formula injection: the values come from user-editable symbol and footprint
fields); the numeric CPL columns are formatted from parsed floats, so a negative coordinate
stays `-1.2000mm`.
"""

from __future__ import annotations

import csv
import io
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from copperchat.board.boardmd import AcceptedWarning
from copperchat.board.metrics import MetricsReport
from copperchat.board.model import BoardModel
from copperchat.board.presets import FabPreset


@dataclass(frozen=True)
class FabCommand:
    """One `kicad-cli` call: argv after the binary, its output (relative to the staging dir)
    and its explicit timeout."""

    args: tuple[str, ...]
    out: str
    timeout_s: float


FAB_TIMEOUTS_S = {"gerbers": 120.0, "drill": 60.0, "pos": 60.0, "bom": 60.0}
EXPORT_3D_TIMEOUT_S = 180.0

GERBER_DIR = "gerbers"
DRILL_DIR = "drill"
POS_CSV = "pos-kicad.csv"  # kicad-cli outputs, converted and removed before the swap
BOM_CSV = "bom-kicad.csv"
BOM_FILE = "bom-jlcpcb.csv"
CPL_FILE = "cpl-jlcpcb.csv"
REPORT_FILE = "fab-report.md"

TECH_LAYERS = ("F.Mask", "B.Mask", "F.SilkS", "B.SilkS", "F.Paste", "B.Paste", "Edge.Cuts")
BOM_FIELDS = "Value,Reference,Footprint,LCSC,MPN"
BOM_GROUP_BY = "Value,Footprint,LCSC,MPN"
BOM_HEADER = ("Comment", "Designator", "Footprint", "LCSC Part #")  # JLC template (call 17)
CPL_HEADER = ("Designator", "Mid X", "Mid Y", "Layer", "Rotation")
ASSEMBLY_NOTE = "BOM/CPL not validated for JLC assembly (rotation offsets, LCSC numbers)"
FINE_PITCH_MM = 0.5  # a pad pitch below this → ENIG recommended (flat pads for fine pitch)
STACKED_PAD_MM = 0.05  # pads closer than this are stacked (one position), not a pitch


def fab_commands(model: BoardModel, board_file: str) -> tuple[FabCommand, ...]:
    """Gerbers (every copper layer + mask, silk, paste of both sides, Edge.Cuts; zones
    refilled on the copy when stale), Excellon drill (mm, PTH/NPTH separate, Gerber X2 map),
    position file and board BOM - in this order, `board_file` relative to the staging dir."""
    layers = ",".join((*model.copper_layers, *TECH_LAYERS))
    pcb = board_file
    return (
        FabCommand(
            ("pcb", "export", "gerbers", "--layers", layers, "--subtract-soldermask",
             "--check-zones", "-o", f"{GERBER_DIR}/", pcb),
            GERBER_DIR,
            FAB_TIMEOUTS_S["gerbers"],
        ),
        FabCommand(
            ("pcb", "export", "drill", "--format", "excellon", "--excellon-units", "mm",
             "--excellon-separate-th", "--generate-map", "--map-format", "gerberx2",
             "-o", f"{DRILL_DIR}/", pcb),
            DRILL_DIR,
            FAB_TIMEOUTS_S["drill"],
        ),
        FabCommand(
            ("pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both",
             "--exclude-dnp", "-o", POS_CSV, pcb),
            POS_CSV,
            FAB_TIMEOUTS_S["pos"],
        ),
        FabCommand(
            ("pcb", "export", "bom", "--fields", BOM_FIELDS, "--labels", BOM_FIELDS,
             "--group-by", BOM_GROUP_BY, "--ref-delimiter", ",", "--ref-range-delimiter", "",
             "--exclude-dnp", "-o", BOM_CSV, pcb),
            BOM_CSV,
            FAB_TIMEOUTS_S["bom"],
        ),
    )  # fmt: skip


# -- BOM / CPL ------------------------------------------------------------------------------

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: str) -> str:
    """A text cell with a leading `'` when a spreadsheet would read it as a formula."""
    return "'" + value if value.startswith(_FORMULA_START) else value


def _rows(text: str) -> list[dict[str, str]]:
    return [
        {k.strip(): v or "" for k, v in row.items() if k is not None}
        for row in csv.DictReader(io.StringIO(text))
    ]


def _footprint_name(fp: str) -> str:
    """`Library:Name` → `Name` (JLC's Footprint column has no library prefix)."""
    return fp.split(":", 1)[1] if ":" in fp else fp


def _designators(refs: str) -> str:
    """kicad-cli's grouped references (`R1,R2` or `R1, R2`) as `R1,R2`."""
    return ",".join(r for r in re.split(r"[,\s]+", refs) if r)


def bom_rows(kicad_bom_csv: str) -> list[tuple[str, str, str, str]]:
    """JLC BOM rows (Comment, Designator, Footprint, LCSC Part #) from `kicad-cli pcb export
    bom` CSV with the labels of `BOM_FIELDS`: Comment = Value, Footprint without the library
    prefix, the 4th column = LCSC if set, else MPN, else empty."""
    out: list[tuple[str, str, str, str]] = []
    for row in _rows(kicad_bom_csv):
        part_no = row.get("LCSC", "") or row.get("MPN", "")
        out.append(
            (
                safe_cell(row.get("Value", "")),
                safe_cell(_designators(row.get("Reference", ""))),
                safe_cell(_footprint_name(row.get("Footprint", ""))),
                safe_cell(part_no),
            )
        )
    return out


def _number(text: str, what: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"kicad-cli wrote a non-number {what}: {text[:40]!r}") from None
    if not math.isfinite(value):
        raise ValueError(f"kicad-cli wrote a non-finite {what}: {text[:40]!r}")
    return value


_SIDES = {"top": "Top", "bottom": "Bottom"}


def cpl_rows(kicad_pos_csv: str) -> list[tuple[str, str, str, str, str]]:
    """JLC CPL rows (Designator, Mid X, Mid Y, Layer, Rotation) from `kicad-cli pcb export
    pos --format csv --units mm` (columns Ref, PosX, PosY, Rot, Side): numbers parsed and
    written as `"%.4fmm"` / `"%.1f"`, layer `Top` / `Bottom`. A non-number or an unknown
    side raises `ValueError` (the export fails rather than writing a wrong CPL)."""
    out: list[tuple[str, str, str, str, str]] = []
    for row in _rows(kicad_pos_csv):
        side = _SIDES.get(row.get("Side", "").casefold())
        if side is None:
            raise ValueError(f"kicad-cli wrote an unknown side {row.get('Side', '')[:20]!r}")
        x = _number(row.get("PosX", ""), "PosX")
        y = _number(row.get("PosY", ""), "PosY")
        rot = _number(row.get("Rot", ""), "Rot")
        out.append((safe_cell(row.get("Ref", "")), f"{x:.4f}mm", f"{y:.4f}mm", side, f"{rot:.1f}"))
    return out


def csv_text(header: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    """The CSV file text (minimal quoting, `\\n` line ends)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


# -- the report -----------------------------------------------------------------------------

_RULE = re.compile(r'\(\s*rule\s+(?:"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.IGNORECASE)


def _comment_lines(lines: Iterable[str]) -> list[str]:
    """The leading run of `#` comment lines (text after the `#`)."""
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if not s.startswith("#"):
            break
        out.append(s.lstrip("#").strip())
    return out


def custom_rules(dru_text: str) -> list[tuple[str, str]]:
    """(name, reason) of every custom rule of a `.kicad_dru`. The reason is the `#` comment
    block right below the rule's header line (where KiCad writes a rule's comments, i.e.
    the `pcb_set_rules` reason) while the rule is still open, else the block right before
    the rule, else empty."""
    out: list[tuple[str, str]] = []
    for m in _RULE.finditer(dru_text):
        name = m.group(1) if m.group(1) is not None else m.group(2)
        header_end = dru_text.find("\n", m.start())
        header = dru_text[m.start() : header_end if header_end >= 0 else len(dru_text)]
        inside: list[str] = []
        if header_end >= 0 and header.count("(") > header.count(")"):  # rule still open
            inside = _comment_lines(dru_text[header_end + 1 :].split("\n"))
        before = dru_text[: m.start()].rstrip(" \t").split("\n")[:-1]
        reason = inside or list(reversed(_comment_lines(reversed(before))))
        out.append((name.replace('\\"', '"'), " ".join(reason).strip()))
    return out


def md(text: object) -> str:
    """A string safe inside one Markdown table cell / line: backslash, `|`, backticks
    escaped, line breaks folded to spaces."""
    s = str(text).replace("\\", "\\\\").replace("|", "\\|").replace("`", "\\`")
    return re.sub(r"[\r\n]+", " ", s)


_COPPER = frozenset({"F.Cu", "B.Cu"})  # SMD pads sit on an outer copper layer


def min_pad_pitch(model: BoardModel) -> float | None:
    """The smallest centre distance between two copper SMD pads of one part (paste-only
    aperture pads and stacked pads, closer than `STACKED_PAD_MM`, excluded); None without
    such a pair."""
    best: float | None = None
    for p in model.parts:
        pads = sorted((q for q in p.pads if q.smd and _COPPER & q.layers), key=lambda q: q.x)
        for i, a in enumerate(pads):
            for b in pads[i + 1 :]:
                if best is not None and b.x - a.x >= best:
                    break
                d = math.hypot(b.x - a.x, b.y - a.y)
                if d >= STACKED_PAD_MM and (best is None or d < best):
                    best = round(d, 4)
    return best


def stencil_sides(model: BoardModel) -> str:
    """ "top only" when every part with an SMD pad sits on top, else "top + bottom"."""
    bottom = any(p.side == "bottom" and any(q.smd for q in p.pads) for p in model.parts)
    return "top + bottom" if bottom else "top only"


def _finish(model: BoardModel) -> str:
    pitch = min_pad_pitch(model)
    if pitch is not None and pitch < FINE_PITCH_MM:
        return f"ENIG (finest SMD pad pitch {pitch:g} mm < {FINE_PITCH_MM:g} mm)"
    return "lead-free HASL" + (f" (finest SMD pad pitch {pitch:g} mm)" if pitch else "")


def _value(v: float | int | None) -> str:
    if v is None:
        return "-"
    return f"{v:g}" if isinstance(v, float) else str(v)


def fab_report(
    model: BoardModel,
    *,
    board_file: str,
    board_hash: str,
    date: str,
    preset: FabPreset | None,
    metrics: MetricsReport,
    accepted: Sequence[AcceptedWarning],
    custom_rules: Sequence[tuple[str, str]],
    files: Sequence[tuple[str, int]],
    swept: Sequence[str] = (),
) -> str:
    """`fab-report.md`: what was exported from which board state, the checks it passed and
    the order notes for JLCPCB. Every injected string is Markdown-escaped (`md`)."""
    o = model.outline
    size = f"{o.w:.1f} x {o.h:.1f} mm" if o.kind != "none" else "no outline"
    thickness = preset.thickness if preset is not None else 1.6
    oz = preset.outer_copper_oz if preset is not None else 1
    lines = [
        "# Fab report (JLCPCB)",
        "",
        f"- Board file: `{md(board_file)}`",
        f"- board_hash: `{md(board_hash)}`",
        f"- Exported (UTC): {md(date)}",
        f"- Layers: {len(model.copper_layers)} copper layers "
        f"({md(', '.join(model.copper_layers))})",
        f"- Outline: {size}",
        "- Rules: "
        + (f"preset {md(preset.id)}" if preset is not None else "custom rules (no fab preset)"),
        "- Stackup: "
        + (
            "; ".join(f"{md(n)} {md(v)}" for n, v in model.stackup)
            if model.stackup
            else "KiCad default stackup"
        ),
        "",
        "## Metrics",
        "",
        f"Stage {md(metrics.stage)}: {metrics.hard_failures} hard failures, "
        f"{metrics.reviews} reviews.",
        "",
        "| Metric | Value | Verdict |",
        "|---|---|---|",
        *(f"| {md(m.id)} | {md(_value(m.value))} | {md(m.verdict)} |" for m in metrics.metrics),
        "",
        "## Accepted warnings (board.md Checks)",
        "",
    ]
    if accepted:
        lines += ["| DRC type | Items | Reason | Accepted by |", "|---|---|---|---|"]
        lines += [
            f"| {md(a.drc_type)} | {md(', '.join(a.items))} | {md(a.reason)} | "
            f"{md(a.accepted_by)} |"
            for a in accepted
        ]
    else:
        lines.append("None.")
    lines += ["", "## Custom rules (.kicad_dru)", ""]
    if custom_rules:
        lines += ["| Rule | Reason |", "|---|---|"]
        lines += [f"| {md(n)} | {md(r) or '-'} |" for n, r in custom_rules]
    else:
        lines.append("None.")
    lines += [
        "",
        "## Order notes",
        "",
        f"- Thickness {thickness:g} mm, {oz:g} oz outer copper",
        f"- Surface finish: {_finish(model)}",
        f"- Stencil: {stencil_sides(model)}",
        "- Upload the zip (gerbers + drill); BOM and CPL are the two CSV files",
        f"- {ASSEMBLY_NOTE}",
        "",
        "## Files",
        "",
        "| File | Bytes |",
        "|---|---|",
        *(f"| {md(name)} | {size_b} |" for name, size_b in files),
    ]
    if swept:
        lines += ["", f"Swept leftovers of earlier exports: {md(', '.join(swept))}"]
    return "\n".join(lines) + "\n"
