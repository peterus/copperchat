"""Read-only footprint tools (spec §7.2-§7.4): `fp_get`, `fp_list_local`, `fp_check`."""

from __future__ import annotations

import asyncio
import difflib
import math
import re
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal, cast

from copperchat.db.repos.footprints import FootprintWriteRepo
from copperchat.errors import DocumentNotOpen, StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools import access
from copperchat.kicad_tools.filtering import natural_key
from copperchat.kicad_tools.footprints.checks import (
    DEFAULT_TOLERANCE,
    CheckInput,
    Expected,
    ExpectedPad,
    run_checks,
    to_result,
)
from copperchat.kicad_tools.footprints.model import (
    MAX_TEXT_CHARS,
    SOURCE_FIELD,
    FootprintGeometry,
    _cap_text,
    review_state,
)
from copperchat.library.service import closest_footprints, library_for
from copperchat.tools.spec import ToolContext, ToolResult

MAX_LIB_ID_CHARS = 256
DETAILS = ("summary", "full")
MAX_QUERY_CHARS = 200
MAX_LIST_LIMIT = 200
DEFAULT_LIST_LIMIT = 50
# fp_check input bounds (plan author call 18)
MAX_SYMBOL_CHARS = 256
MAX_PAD_NUMBER_CHARS = 16
MAX_EXPECTED_PADS = 512
MAX_ALLOW_UNMATCHED = 64
MAX_COORD_MM = 500.0
MAX_SIZE_MM = 200.0
MAX_DRILL_MM = 50.0
MIN_TOLERANCE_MM = 0.001
MAX_TOLERANCE_MM = 1.0
REFERENCE_RE = re.compile(r"^[A-Za-z#]+[0-9]+$")


def _invalid(what: str, why: str, suggestion: str) -> StructuredToolError:
    return StructuredToolError(what, why, suggestion, code="invalid_argument")


def require_enabled(ctx: ToolContext) -> None:
    """Defence in depth: the profiles drop every `fp_*` tool when the switch is off."""
    if not ctx.settings.footprint_tools:
        raise StructuredToolError(
            "Footprint tools are disabled",
            "COPPERCHAT_FOOTPRINT_TOOLS is off on this server",
            "Pick footprints with lib_search_footprints instead.",
            code="footprint_tools_disabled",
        )


def parse_lib_id(value: Any) -> tuple[str, str, str]:
    """(lib_id, nickname, name) of a `<nickname>:<name>` argument, else invalid_argument."""
    if not isinstance(value, str) or len(value) > MAX_LIB_ID_CHARS:
        raise _invalid(
            "Invalid lib_id",
            f"lib_id must be a string of at most {MAX_LIB_ID_CHARS} characters",
            "e.g. 'Inductor_SMD:L_Bourns-SRN4018'",
        )
    lib_id = value.strip()
    nickname, sep, name = lib_id.partition(":")
    if not sep or not nickname or not name:
        raise _invalid(
            f"Invalid lib_id {lib_id[:80]!r}",
            "expected <library nickname>:<footprint name>",
            "e.g. 'Inductor_SMD:L_Bourns-SRN4018'; find ids with lib_search_footprints",
        )
    return lib_id, nickname, name


async def pending_lib_ids(ctx: ToolContext) -> frozenset[str]:
    """Lib ids with a `pending` write-journal row of this project (FP-C3: such a
    footprint is `unreviewed`). Read-only; empty without the editing service (no DB,
    e.g. plain tool contexts outside the run engine). `row_add` rows carry the bare
    nickname, so they never match a `<nick>:<name>`."""
    if ctx.editing is None:
        return frozenset()
    rows = await FootprintWriteRepo(ctx.editing.db).pending(ctx.project.id)
    return frozenset(r.lib_id for r in rows)


async def _geometry(
    ctx: ToolContext, lib_id: str, nickname: str, name: str, notes: list[dict[str, Any]]
) -> FootprintGeometry:
    """The footprint's geometry, else `not_found` with the closest lib ids."""
    svc = library_for(ctx.session)
    geometry = await svc.footprint_geometry(lib_id, notes)
    if geometry is None:
        close = await closest_footprints(svc, nickname, name)
        raise StructuredToolError(
            f"Footprint {lib_id!r} is not in the libraries",
            "unknown footprint (format <library nickname>:<footprint name>, case-sensitive)",
            f"Closest matches: {close}" if close else "Use lib_search_footprints.",
            code="not_found",
            details={"closest": close},
        )
    return geometry


async def fp_get(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """One footprint's geometry (spec §7.2): `FootprintGeometry.to_dict(detail)` plus
    `source`, `review` (local footprints only, else null) and `notes`."""
    lib_id, nickname, name = parse_lib_id(args.get("lib_id"))
    detail = args.get("detail", "summary")
    if detail not in DETAILS:
        raise _invalid(
            "Invalid detail", "detail must be 'summary' or 'full'", "omit it for 'summary'"
        )
    require_enabled(ctx)
    notes: list[dict[str, Any]] = []
    geometry = await _geometry(ctx, lib_id, nickname, name, notes)
    data = geometry.to_dict(cast(Literal["summary", "full"], detail))
    source = geometry.fields.get(SOURCE_FIELD)
    if source is not None:
        source, cut = _cap_text(source)
        if cut:
            data["truncated"] = True
            extra = f"source cut at {MAX_TEXT_CHARS} chars"
            prior = data.get("note")
            data["note"] = f"{prior} Also {extra}." if prior else f"Output truncated: {extra}."
    data["source"] = source
    if geometry.scope == "local":
        pending = geometry.lib_id in await pending_lib_ids(ctx)
        data["review"] = review_state(geometry, pending=pending)
    else:
        data["review"] = None
    data["notes"] = notes
    return ToolResult(data=data)


def _query(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, str) or len(value) > MAX_QUERY_CHARS:
        raise _invalid(
            "Invalid query",
            f"query must be a string of at most {MAX_QUERY_CHARS} characters",
            "e.g. 'SRN3012' or 'inductor', or omit it",
        )
    return value.casefold().split()


def _list_limit(value: Any) -> int:
    if value is None:
        return DEFAULT_LIST_LIMIT
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid("Invalid limit", "limit must be an integer", f"1..{MAX_LIST_LIMIT}")
    if not 1 <= value <= MAX_LIST_LIMIT:
        raise _invalid("Invalid limit", f"limit must be 1..{MAX_LIST_LIMIT}", "e.g. 50")
    return value


def _footprint_users(nickname: str) -> Any:
    """lib_id -> sorted references of the open project's symbols whose Footprint field
    names a footprint of `nickname` (the symbol reader of sch_list_symbols); empty for a
    board-only project (no schematic open)."""

    def footprint_users(h: KicadHandle) -> dict[str, list[str]]:
        try:
            schematic = h.schematic()
        except DocumentNotOpen:
            return {}
        users: dict[str, set[str]] = {}
        for sym in schematic.get_symbols():
            d = access.symbol_dict(sym)
            footprint = d["footprint"].strip()
            if footprint.partition(":")[0] == nickname:
                users.setdefault(footprint, set()).add(d["ref"])
        return {k: sorted(v, key=natural_key) for k, v in users.items()}

    return footprint_users


def _no_library(notes: list[dict[str, Any]], kind: str, message: str) -> ToolResult:
    notes.append({"kind": kind, "message": message})
    return ToolResult(data={"library": None, "footprints": [], "truncated": False, "notes": notes})


async def fp_list_local(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """The footprints of the repo's local library (spec §7.3). Without a project row
    for it the result is `library: null` with a note, and nothing is activated."""
    terms = _query(args.get("query"))
    limit = _list_limit(args.get("limit"))
    require_enabled(ctx)
    svc = library_for(ctx.session)
    notes: list[dict[str, Any]] = []
    info = await svc.local_library(require_root=True)
    assert info is not None  # require_root raises repo_root_unknown instead
    lib = info.lib
    if info.row_conflict:
        return _no_library(notes, "library_conflict", info.row_conflict)
    if not info.row_present:
        on_disk = len(await asyncio.to_thread(svc.local_names_on_disk, lib))
        message = (
            f"This project has no local footprint library yet ({lib.nickname}); the "
            "first fp_create adds it."
        )
        if on_disk:
            message += (
                f" The repo's library directory already holds {on_disk} footprint(s) "
                "from another project; they become usable here once a footprint tool "
                "adds the row."
            )
        return _no_library(notes, "no_local_library", message)
    geometries = await svc.local_footprints(lib, notes)
    pending = await pending_lib_ids(ctx)
    users = await ctx.session.call(_footprint_users(lib.nickname))
    rows: list[dict[str, Any]] = []
    for g in sorted(geometries, key=lambda g: natural_key(g.lib_id)):
        name = g.lib_id.partition(":")[2]
        haystack = f"{name} {g.description}".casefold()
        if not all(t in haystack for t in terms):
            continue
        # the filter above matches the full text; only the output is capped
        description, cut_description = _cap_text(g.description)
        source = g.fields.get(SOURCE_FIELD)
        cut_source = False
        if source is not None:
            source, cut_source = _cap_text(source)
        row: dict[str, Any] = {
            "name": name,
            "lib_id": g.lib_id,
            "description": description,
            "pad_count": len(g.pads),
            "needs_user_review": review_state(g, pending=g.lib_id in pending),
            "source": source,
            "used_by": users.get(g.lib_id, []),
        }
        if cut_description or cut_source:
            row["truncated"] = True
        rows.append(row)
    if any(r.get("truncated") for r in rows[:limit]):
        notes.append(
            {
                "kind": "strings_truncated",
                "message": f"descriptions/sources longer than {MAX_TEXT_CHARS} chars are "
                "cut ('…' appended; those rows carry `truncated: true`)",
            }
        )
    return ToolResult(
        data={
            "library": {
                "nickname": lib.nickname,
                "uri": lib.uri,
                "path_rel_repo": lib.dir.relative_to(lib.repo_root).as_posix(),
            },
            "footprints": rows[:limit],
            "truncated": len(rows) > limit,
            "notes": notes,
        }
    )


# --- fp_check (spec §7.4) -------------------------------------------------------------


def _keys(value: Any, what: str, allowed: set[str], required: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid(f"Invalid {what}", f"{what} must be an object", "see the tool schema")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _invalid(
            f"Invalid {what}", f"unknown key(s) {unknown}", f"allowed: {sorted(allowed)}"
        )
    missing = sorted(required - set(value))
    if missing:
        raise _invalid(f"Invalid {what}", f"missing key(s) {missing}", "see the tool schema")
    return value


def _as_float(value: Any) -> float:
    """A JSON number as float; NaN for non-numbers, bools and ints too large for a
    float (`10**400` raises OverflowError in `float()`), so callers reject them."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return math.nan
    try:
        return float(value)
    except OverflowError:
        return math.nan


def _number(value: Any, what: str, lo: float, hi: float, *, open_lo: bool = False) -> float:
    v = _as_float(value)
    if not (math.isfinite(v) and (v > lo if open_lo else v >= lo) and v <= hi):
        bound = f"{lo} < {what} <= {hi}" if open_lo else f"{lo} <= {what} <= {hi}"
        raise _invalid(f"Invalid {what}", f"{what} must be a finite number, {bound} (mm)", bound)
    return v


def _pair(
    value: Any, what: str, lo: float, hi: float, *, open_lo: bool = False
) -> tuple[float, float]:
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise _invalid(
            f"Invalid {what}", f"{what} must be [x, y] (two numbers, mm)", "e.g. [1.0, 0.5]"
        )
    return (
        _number(value[0], what, lo, hi, open_lo=open_lo),
        _number(value[1], what, lo, hi, open_lo=open_lo),
    )


def _pad_number(value: Any, what: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_PAD_NUMBER_CHARS:
        raise _invalid(
            f"Invalid {what}",
            f"a pad number is a string of 1..{MAX_PAD_NUMBER_CHARS} characters",
            "e.g. '1' or 'EP'",
        )
    return value


def _size(value: Any, what: str) -> tuple[float, float]:
    return _pair(value, what, 0.0, MAX_SIZE_MM, open_lo=True)


def _expected(value: Any) -> Expected:
    exp = _keys(value, "expected", {"pads", "pitch", "courtyard", "body", "tolerance_mm"}, {"pads"})
    raw = exp["pads"]
    if not isinstance(raw, list) or len(raw) > MAX_EXPECTED_PADS:
        raise _invalid(
            "Invalid expected.pads",
            f"expected.pads must be a list of at most {MAX_EXPECTED_PADS} pads",
            "one entry per datasheet pad",
        )
    pads: list[ExpectedPad] = []
    for i, item in enumerate(raw):
        what = f"expected.pads[{i}]"
        p = _keys(item, what, {"number", "at", "size", "drill"}, {"number", "at", "size"})
        drill = p.get("drill")
        pads.append(
            ExpectedPad(
                number=_pad_number(p["number"], f"{what}.number"),
                at=_pair(p["at"], f"{what}.at", -MAX_COORD_MM, MAX_COORD_MM),
                size=_size(p["size"], f"{what}.size"),
                drill=None
                if drill is None
                else _number(drill, f"{what}.drill", 0.0, MAX_DRILL_MM, open_lo=True),
            )
        )
    pitch, courtyard, body = exp.get("pitch"), exp.get("courtyard"), exp.get("body")
    tolerance = exp.get("tolerance_mm")
    return Expected(
        pads=tuple(pads),
        pitch=None
        if pitch is None
        else _number(pitch, "expected.pitch", 0.0, MAX_SIZE_MM, open_lo=True),
        courtyard=None if courtyard is None else _size(courtyard, "expected.courtyard"),
        body=None if body is None else _size(body, "expected.body"),
        tolerance_mm=DEFAULT_TOLERANCE
        if tolerance is None
        else _number(tolerance, "expected.tolerance_mm", MIN_TOLERANCE_MM, MAX_TOLERANCE_MM),
    )


def _allow_unmatched(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or len(value) > MAX_ALLOW_UNMATCHED:
        raise _invalid(
            "Invalid allow_unmatched_pads",
            f"allow_unmatched_pads must be a list of at most {MAX_ALLOW_UNMATCHED} pad numbers",
            "e.g. ['10', '11'] for shell pads",
        )
    return frozenset(_pad_number(v, "allow_unmatched_pads item") for v in value)


def _symbol(value: Any) -> str | None:
    """A `lib:Name` symbol id or a placed reference (`R201`, `#PWR01`), else invalid."""
    if value is None:
        return None
    if isinstance(value, str) and len(value) <= MAX_SYMBOL_CHARS:
        symbol = value.strip()
        nickname, sep, name = symbol.partition(":")
        if (sep and nickname and name) or REFERENCE_RE.match(symbol):
            return symbol
    raise _invalid(
        "Invalid symbol",
        f"symbol must be a library symbol id <nickname>:<name> or a placed reference "
        f"like R201 (at most {MAX_SYMBOL_CHARS} characters)",
        "e.g. 'Device:L' or 'L201'",
    )


POLARIZED_NAME_RE = re.compile(r"^(?:C_Polarized|CP)")  # KiCad names, case-sensitive
POLARIZED_WORDS = ("polarized", "polarised", "electrolytic", "tantalum")
# KiCad's Device:C is "Unpolarized capacitor"; bipolar electrolytics are not polarised
UNPOLARIZED_WORDS = ("unpolar", "non-polar", "nonpolar", "non polar", "bipolar")


def is_polarized(
    pins: dict[str, str], name: str = "", description: str = "", keywords: str = ""
) -> bool:
    """Exactly 2 pins, and either their names differ and are neither `~` (or empty) nor
    equal to their number (a diode's K/A, not a resistor's 1/2), or the symbol is a
    polarised capacitor whose pins are both `~` (spec §8): its library name starts with
    `C_Polarized` or `CP` (`CP`, `CP_Small`, ...), or its description or keywords say
    polarized / polarised / electrolytic / tantalum and not unpolarised / non-polarised /
    bipolar (KiCad's plain `Device:C` is "Unpolarized capacitor")."""
    if len(pins) != 2:
        return False
    names = list(pins.values())
    if names[0] != names[1] and all(
        pin and pin != "~" and pin != number for number, pin in pins.items()
    ):
        return True
    text = f"{description} {keywords}".casefold()
    if POLARIZED_NAME_RE.match(name):
        return True
    if any(w in text for w in UNPOLARIZED_WORDS):
        return False
    return any(w in text for w in POLARIZED_WORDS)


@dataclass(frozen=True)
class PlacedSymbol:
    pins: dict[str, str]  # pin number -> name over every unit
    name: str = ""  # library entry name of the definition
    description: str = ""
    keywords: str = ""


def _placed_pins(ref: str, h: KicadHandle) -> tuple[PlacedSymbol | None, list[str] | None]:
    """(the placed symbol `ref`'s pins over every unit plus its definition's name,
    description and keywords, or None when not placed; the placed references, or None
    without an open schematic)."""
    try:
        schematic = h.schematic()
    except DocumentNotOpen:
        return None, None
    refs: list[str] = []
    for sym in schematic.get_symbols():
        reference = str(sym.reference)
        refs.append(reference)
        if reference != ref:
            continue
        definition = sym.definition
        pins: dict[str, str] = {}
        for child in definition.items:  # every unit and body style of the definition
            if child.kind != "SchematicPin":
                continue
            pin: Any = child.item
            if str(pin.number):
                pins.setdefault(str(pin.number), str(pin.name))
        placed = PlacedSymbol(
            pins,
            str(definition.id.name),
            str(definition.description_field.text.value),
            str(definition.keywords),
        )
        return placed, refs
    return None, refs


async def symbol_pins(ctx: ToolContext, symbol: str) -> tuple[list[str], bool]:
    """(pin numbers, polarized) of a library symbol (`lib:Name`, via
    `LibraryService.get_symbol`) or of a placed reference; all units' pins."""
    if REFERENCE_RE.match(symbol):
        placed, refs = await ctx.session.call(partial(_placed_pins, symbol))
        if placed is None:
            if refs is None:
                why, suggestion = "no schematic is open", "Pass the library symbol id instead."
            else:
                close = difflib.get_close_matches(symbol, sorted(set(refs)), n=5, cutoff=0.5)
                why = "no symbol with this reference is placed in the open schematic"
                suggestion = (
                    f"Did you mean: {', '.join(close)}?"
                    if close
                    else "Use sch_list_symbols, or pass the library symbol id instead."
                )
            raise StructuredToolError(
                f"Symbol {symbol} not found", why, suggestion, code="not_found"
            )
        pins = placed.pins
        polarized = is_polarized(pins, placed.name, placed.description, placed.keywords)
    else:
        detail = await library_for(ctx.session).get_symbol(symbol)
        if not detail.pins_known:
            raise StructuredToolError(
                f"Could not read the pins of {symbol}",
                "a derived symbol's pins need a probe placement, which this read-only "
                "session cannot make",
                "Pass the reference of a placed instance (e.g. 'U202') or the base symbol.",
                code="library_error",
            )
        pins = {}
        for pin in detail.pins:
            if pin.number:
                pins.setdefault(pin.number, pin.name)
        name = symbol.partition(":")[2]
        polarized = is_polarized(pins, name, detail.description, detail.keywords)
    return sorted(pins, key=natural_key), polarized


async def fp_check(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """The spec §8 rules on one footprint (spec §7.4): `{lib_id, passed, counts,
    findings, geometry_hash, truncated, notes}`; read-only, also for stock footprints."""
    unknown = sorted(set(args) - {"lib_id", "symbol", "expected", "allow_unmatched_pads"})
    if unknown:
        raise _invalid("Invalid arguments", f"unknown key(s) {unknown}", "see the tool schema")
    lib_id, nickname, name = parse_lib_id(args.get("lib_id"))
    symbol = _symbol(args.get("symbol"))
    # absent -> no expectations; an explicit null is rejected (the schema says object)
    expected = _expected(args["expected"]) if "expected" in args else None
    allow = _allow_unmatched(args.get("allow_unmatched_pads"))
    require_enabled(ctx)
    notes: list[dict[str, Any]] = []
    geometry = await _geometry(ctx, lib_id, nickname, name, notes)
    pins, polarized = await symbol_pins(ctx, symbol) if symbol else (None, False)
    local = geometry.scope == "local"
    inp = CheckInput(
        geometry=geometry,
        pins=pins,
        polarized=polarized,
        expected=expected,
        allow_unmatched=allow,
        local=local,
        pending=local and geometry.lib_id in await pending_lib_ids(ctx),
    )
    report = await asyncio.to_thread(run_checks, inp)
    return ToolResult(data={"lib_id": geometry.lib_id, **to_result(report), "notes": notes})
