"""`board.md` table parser (B-C6): the machine-readable tables of the board design document
(spec §8.4) as frozen rows. Pure and design-agnostic, no kipy import; `parse_board_md` never
raises — unparseable rows are skipped and reported in `BoardDoc.errors`.

Tables are recognised by their header cell set (casefolded) in any `## ` section; HTML comments
(the template's table hints) are removed before parsing. The Summary section's `Assembly: …;
sides: top|top+bottom` line gives the allowed sides, its `Preset: <id>` line the fab preset."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, get_args

from copperchat.board.model import Side
from copperchat.design_docs.paths import DocError

if TYPE_CHECKING:
    from copperchat.tools.spec import ToolContext

__all__ = [
    "MAX_BYTES",
    "MAX_ERRORS",
    "AcceptedWarning",
    "BlockRow",
    "BoardDoc",
    "ClassOverride",
    "Edge",
    "EdgeRow",
    "NetClassRow",
    "PartClass",
    "Side",
    "parse_board_md",
    "read_board_doc",
]

PartClass = Literal[
    "anchor",
    "connector",
    "decoupling_cap",
    "switching_regulator",
    "inductor",
    "crystal",
    "module_with_antenna",
    "mounting_hole",
    "fiducial",
    "test_point",
    "other",
]
Edge = Literal["top", "bottom", "left", "right"]

PART_CLASSES: frozenset[str] = frozenset(get_args(PartClass))
EDGES: frozenset[str] = frozenset(get_args(Edge))
MAX_BYTES = 256 * 1024
MAX_ERRORS = 50
MAX_REF_CHARS = 32  # a ref longer than this is no KiCad reference (refs maxLength 32)

_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_TABLE_SEP = re.compile(r"^:?-+:?$")
_LIST_SPLIT = re.compile(r"[,\s]+")
_NUMBER = re.compile(r"(\d+(?:\.\d*)?|\.\d+)\s*(?:mm)?")
_NONE_CELLS = frozenset({"", "-", "\u2014", "\u2013"})  # empty, hyphen, em and en dash
_FIRST_WORD = re.compile(r"[a-z]+")
_ASSEMBLY = re.compile(r"^[\W_]*assembly\s*:", re.IGNORECASE)
_SIDES = re.compile(r"\bsides\s*:\s*([^\s;,]*)", re.IGNORECASE)
_PRESET = re.compile(r"^[\W_]*preset\s*:(.*)$", re.IGNORECASE)
_SIDE_VALUES: dict[str, frozenset[Side]] = {
    "top": frozenset({"top"}),
    "top+bottom": frozenset({"top", "bottom"}),
    "bottom+top": frozenset({"top", "bottom"}),
}


@dataclass(frozen=True)
class EdgeRow:
    ref: str
    edge: Edge | None
    position: str
    reason: str


@dataclass(frozen=True)
class BlockRow:
    name: str
    refs: tuple[str, ...]
    region: str
    notes: str


@dataclass(frozen=True)
class ClassOverride:
    ref: str
    part_class: PartClass
    reason: str


@dataclass(frozen=True)
class NetClassRow:
    name: str
    width: float | None
    clearance: float | None
    via: str
    nets: tuple[str, ...]


@dataclass(frozen=True)
class AcceptedWarning:
    drc_type: str
    items: tuple[str, ...]
    reason: str
    accepted_by: str


@dataclass(frozen=True)
class BoardDoc:
    edges: tuple[EdgeRow, ...] = ()
    blocks: tuple[BlockRow, ...] = ()
    class_overrides: tuple[ClassOverride, ...] = ()
    net_classes: tuple[NetClassRow, ...] = ()
    accepted: tuple[AcceptedWarning, ...] = ()
    allowed_sides: frozenset[Side] = frozenset({"top"})
    preset: str | None = None
    errors: tuple[str, ...] = ()


class _RowError(ValueError):
    """A row that is skipped; the message is the `<why>` of the error line."""


def _split_list(cell: str) -> tuple[str, ...]:
    return tuple(p for p in _LIST_SPLIT.split(cell) if p)


def _ref_list(cell: str, notes: list[str]) -> tuple[str, ...]:
    """The refs of a list cell; refs over MAX_REF_CHARS are dropped with a note each."""
    refs = _split_list(cell)
    for r in refs:
        if len(r) > MAX_REF_CHARS:
            notes.append(f"ref longer than {MAX_REF_CHARS} characters dropped: {r!r}")
    return tuple(r for r in refs if len(r) <= MAX_REF_CHARS)


def _ref(cell: str) -> str:
    ref = _required(cell, "ref")
    if len(ref) > MAX_REF_CHARS:
        raise _RowError(f"ref longer than {MAX_REF_CHARS} characters: {ref!r}")
    return ref


def _number(cell: str, what: str) -> float | None:
    if cell in _NONE_CELLS:
        return None
    m = _NUMBER.fullmatch(cell.casefold())
    if m is None:
        raise _RowError(f"unparseable {what} {cell!r}")
    return float(m.group(1))


def _required(cell: str, what: str) -> str:
    if not cell:
        raise _RowError(f"empty {what}")
    return cell


def _edge_row(c: dict[str, str], notes: list[str]) -> EdgeRow:
    position = c["edge/position"]
    m = _FIRST_WORD.match(position.casefold())
    word = m.group(0) if m else ""
    edge: Edge | None = word if word in EDGES else None  # type: ignore[assignment]
    return EdgeRow(_ref(c["ref"]), edge, position, c["reason"])


def _block_row(c: dict[str, str], notes: list[str]) -> BlockRow:
    name = _required(c["block"], "block name")
    return BlockRow(name, _ref_list(c["parts"], notes), c["region"], c["notes"])


def _class_override(c: dict[str, str], notes: list[str]) -> ClassOverride:
    ref = _ref(c["ref"])
    part_class = re.sub(r"[\s\-]+", "_", c["class"].casefold())
    if part_class not in PART_CLASSES:
        raise _RowError(f"unknown class {part_class!r}")
    return ClassOverride(ref, part_class, c["reason"])  # type: ignore[arg-type]


def _net_class_row(c: dict[str, str], notes: list[str]) -> NetClassRow:
    return NetClassRow(
        _required(c["class"], "class name"),
        _number(c["width"], "width"),
        _number(c["clearance"], "clearance"),
        c["via"],
        _split_list(c["nets"]),
    )


def _accepted(c: dict[str, str], notes: list[str]) -> AcceptedWarning:
    drc_type = re.sub(r"\s+", "_", _required(c["drc type"], "DRC type").casefold())
    drc_type = drc_type.removeprefix("drce_")
    if not drc_type:
        raise _RowError("empty DRC type")
    items = _ref_list(c["items/refs"], notes)
    return AcceptedWarning(drc_type, items, c["reason"], c["accepted by"])


_Row = EdgeRow | BlockRow | ClassOverride | NetClassRow | AcceptedWarning
# (cells by header, notes) -> row; a note is an error line for a row that is still kept
_RowParser = Callable[[dict[str, str], list[str]], _Row]
_TABLES: dict[frozenset[str], _RowParser] = {
    frozenset({"ref", "edge/position", "reason"}): _edge_row,
    frozenset({"block", "parts", "region", "notes"}): _block_row,
    frozenset({"ref", "class", "reason"}): _class_override,
    frozenset({"class", "width", "clearance", "via", "nets"}): _net_class_row,
    frozenset({"drc type", "items/refs", "reason", "accepted by"}): _accepted,
}


def _cells(stripped: str) -> list[str]:
    inner = stripped[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [c.strip() for c in inner.split("|")]


def parse_board_md(text: str) -> BoardDoc:
    """The board.md tables and Summary lines as a `BoardDoc`; never raises."""
    errors: list[str] = []
    raw = text.encode("utf-8", errors="replace")
    if len(raw) > MAX_BYTES:
        text = raw[:MAX_BYTES].decode("utf-8", errors="ignore")
        errors.append("board.md is over 256 KiB: only the first 256 KiB were parsed")
    text = _COMMENT.sub("", text)

    rows: list[_Row] = []
    sides: frozenset[Side] = frozenset({"top"})
    preset: str | None = None
    section = "(no section)"
    # Current recognised table: (parser, header columns, data row count); None otherwise.
    table: tuple[_RowParser, list[str], int] | None = None
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if line.startswith("## "):
            section = line[3:].strip().casefold()
            in_table, table = False, None
            continue
        if not stripped.startswith("|"):
            in_table, table = False, None
            if section == "summary":
                if _ASSEMBLY.match(stripped) and (m := _SIDES.search(stripped)):
                    value = m.group(1).casefold().rstrip(".")
                    if value in _SIDE_VALUES:
                        sides = _SIDE_VALUES[value]
                    else:
                        sides = frozenset({"top"})
                        errors.append(
                            f"summary: sides {value!r} is not top or top+bottom; using top"
                        )
                elif m := _PRESET.match(stripped):
                    preset = m.group(1).strip().strip("`*").strip().casefold() or None
            continue
        cells = _cells(stripped)
        if not in_table:
            in_table = True
            header = [c.casefold() for c in cells]
            if len(set(header)) == len(header) and (parse := _TABLES.get(frozenset(header))):
                table = (parse, header, 0)
            continue
        if table is None or all(_TABLE_SEP.fullmatch(c) for c in cells if c):
            continue
        parse, header, n = table
        n += 1
        table = (parse, header, n)
        if not any(cells):
            continue
        try:
            if len(cells) != len(header):
                raise _RowError(f"{len(cells)} cells, expected {len(header)}")
            notes: list[str] = []
            rows.append(parse(dict(zip(header, cells, strict=True)), notes))
            errors += [f"{section} row {n}: {note}" for note in notes]
        except _RowError as exc:
            errors.append(f"{section} row {n}: {exc}")

    if len(errors) > MAX_ERRORS:
        errors = [*errors[:MAX_ERRORS], f"{len(errors) - MAX_ERRORS} more errors omitted"]
    return BoardDoc(
        edges=tuple(r for r in rows if isinstance(r, EdgeRow)),
        blocks=tuple(r for r in rows if isinstance(r, BlockRow)),
        class_overrides=tuple(r for r in rows if isinstance(r, ClassOverride)),
        net_classes=tuple(r for r in rows if isinstance(r, NetClassRow)),
        accepted=tuple(r for r in rows if isinstance(r, AcceptedWarning)),
        allowed_sides=sides,
        preset=preset,
        errors=tuple(errors),
    )


async def read_board_doc(ctx: ToolContext) -> BoardDoc:
    """The project's parsed board.md; an empty `BoardDoc` without docs or without the file."""
    if ctx.docs is None:
        return BoardDoc()
    try:
        body = (await ctx.docs.read(ctx.project, "board.md")).body
    except DocError:
        return BoardDoc()
    return parse_board_md(body)
