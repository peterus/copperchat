"""Tool provider: library search/inspection (`lib_*`) and `parts_get_notes` (phase E3)."""

from __future__ import annotations

import asyncio
from typing import Any

from copperchat.library.service import MAX_LIMIT, library_for
from copperchat.parts import InvalidMpn, mpn_dirname, part_dir
from copperchat.projects import Project
from copperchat.tools import StructuredToolError, ToolContext, ToolResult, ToolSpec

# How long a lib_* call waits for the background warm-up before answering
# `libraries_loading` (the warm-up takes ~10-15 s after the project opened).
READY_WAIT_S = 30.0
MAX_LIB_ID_CHARS = 256
# notes.md returned to the agent: at most this much (a cloned project may ship any file).
MAX_NOTES_RESULT_BYTES = 64 * 1024

_QUERY = {"type": "string", "minLength": 1, "maxLength": 200}
_LIMIT = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_LIMIT,
    "description": f"Maximum number of results (default 20, at most {MAX_LIMIT}).",
}


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


def _library(args: dict[str, Any]) -> str | None:
    library = args.get("library")
    if library is None or library == "":
        return None
    if not isinstance(library, str) or len(library) > 128:
        raise StructuredToolError(
            "Invalid library",
            "library must be a library nickname (string, at most 128 characters)",
            "e.g. 'Device' or 'Resistor_SMD', or omit it",
            code="invalid_argument",
        )
    return library


async def _search_symbols(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    svc = library_for(ctx.session)
    await svc.wait_ready(READY_WAIT_S)
    query = str(args.get("query", ""))
    library = _library(args)
    rows = await svc.search_symbols(query, library, args.get("limit", 20))
    data: dict[str, Any] = {
        "query": query.strip(),
        "library": library,
        "results": rows,
        "note": "pin_count null = derived symbol, lib_get_symbol shows its pins; "
        "scope project = the project's own library",
    }
    if not rows:
        data["hint"] = "No match: try fewer or more general words, or a part-number prefix."
    return ToolResult(data=data)


async def _get_symbol(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    svc = library_for(ctx.session)
    await svc.wait_ready(READY_WAIT_S)
    lib_id = args.get("lib_id")
    if not isinstance(lib_id, str) or not 3 <= len(lib_id) <= MAX_LIB_ID_CHARS:
        raise StructuredToolError(
            "Invalid lib_id",
            f"lib_id must be a string of 3..{MAX_LIB_ID_CHARS} characters",
            "e.g. 'Device:R'; find ids with lib_search_symbols",
            code="invalid_argument",
        )
    detail = await svc.get_symbol(lib_id)
    data = detail.to_dict()
    data["note"] = (
        "offset_mm = pin connection point relative to the symbol origin (mm, y down) at "
        "rotation 0; direction = where the pin points away from the body; body_size_mm = "
        "unit 1 at rotation 0"
    )
    if not detail.pins_known:
        data["note"] += (
            "; pins unknown: this derived symbol needs a probe placement, which a "
            "read-only project does not allow"
        )
    return ToolResult(data=data)


async def _search_footprints(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    svc = library_for(ctx.session)
    query = str(args.get("query", ""))
    library = _library(args)
    rows, failed = await svc.search_footprints_detailed(query, library, args.get("limit", 20))
    data: dict[str, Any] = {"query": query.strip(), "library": library, "results": rows}
    if library is None:
        suggested = await svc.suggest_footprint_libraries(query)
        data["searched_libraries"] = svc.loaded_footprint_libraries()
        if failed:
            data["failed_libraries"] = failed
        data["libraries"] = suggested or sorted(await svc.footprint_libraries())
        data["note"] = (
            "Without `library` only the libraries whose name matches the query words (and "
            "libraries searched before) are searched; pass `library` (see `libraries`) to "
            "search one library completely."
        )
    return ToolResult(data=data)


def _read_notes_capped(project: Project, mpn: str) -> tuple[str, bool]:
    """notes.md, read only up to MAX_NOTES_RESULT_BYTES (same checks as parts.read_notes:
    no symlinks, inside docs/parts)."""
    path = part_dir(project, mpn) / "notes.md"
    if path.is_symlink() or not path.is_file():
        return "", False
    with path.open("rb") as f:
        raw = f.read(MAX_NOTES_RESULT_BYTES + 1)
    truncated = len(raw) > MAX_NOTES_RESULT_BYTES
    return raw[:MAX_NOTES_RESULT_BYTES].decode("utf-8", errors="ignore"), truncated


async def _parts_get_notes(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    mpn = str(args.get("mpn", ""))
    try:
        name = mpn_dirname(mpn)
        text, truncated = await asyncio.to_thread(_read_notes_capped, ctx.project, mpn)
    except InvalidMpn as exc:
        raise StructuredToolError(
            "Invalid part number",
            exc.message,
            "Pass the bare manufacturer part number, e.g. TMP102AIDRLR.",
            code="invalid_argument",
        ) from exc
    if not text.strip():
        return ToolResult(
            data={
                "mpn": name,
                "researched": False,
                "note": "No notes yet: call research_part before relying on this part.",
            }
        )
    data: dict[str, Any] = {"mpn": name, "researched": True, "notes": text}
    if truncated:
        data["truncated"] = True
    return ToolResult(data=data)


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        "lib_search_symbols",
        "Search the KiCad symbol libraries (project libraries first) by name, keywords and "
        "description. Returns lib_id, description, keywords, unit and pin count, default "
        "footprint and footprint filters. Use part numbers or function words, e.g. 'LM358', "
        "'LDO 3.3V', 'resistor'.",
        _obj(
            {
                "query": _QUERY,
                "library": {
                    "type": "string",
                    "description": "Only this library nickname, e.g. 'Device'.",
                },
                "limit": _LIMIT,
            },
            ["query"],
        ),
        _search_symbols,
    ),
    ToolSpec(
        "lib_get_symbol",
        "One library symbol by lib_id: pins (number, name, electrical type, unit, offset "
        "from the origin in mm, outward direction), unit count, body styles, reference "
        "prefix, default footprint and filters, datasheet, body size in mm, and for parts "
        "with at most 4 pins `pin_directions_by_rotation` (read it before rotating "
        "polarised parts such as LEDs and diodes).",
        _obj(
            {"lib_id": {"type": "string", "minLength": 3, "maxLength": MAX_LIB_ID_CHARS}},
            ["lib_id"],
        ),
        _get_symbol,
    ),
    ToolSpec(
        "lib_search_footprints",
        "Search KiCad footprint libraries (loaded on demand, one library at a time): "
        "lib_id, description, pad count, SMD pad count and chip package (e.g. 0805). Pass "
        "`library` (e.g. 'Resistor_SMD') to search one library completely. Not available "
        "in projects without a board.",
        _obj(
            {
                "query": _QUERY,
                "library": {
                    "type": "string",
                    "description": "Footprint library nickname, e.g. 'Resistor_SMD'.",
                },
                "limit": _LIMIT,
            },
            ["query"],
        ),
        _search_footprints,
    ),
    ToolSpec(
        "parts_get_notes",
        "Read the notes of a researched part (docs/parts/<MPN>/notes.md: pinout, unused "
        "pins, application circuit, external parts, layout, KiCad symbol/footprint). "
        "Returns researched=false when there are none yet - then call research_part.",
        _obj(
            {"mpn": {"type": "string", "minLength": 1, "maxLength": 64}},
            ["mpn"],
        ),
        _parts_get_notes,
        requires_kicad=False,
    ),
]
