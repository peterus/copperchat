"""Tool provider: footprint tools (`fp_*`, footprints spec §7)."""

from __future__ import annotations

from typing import Any

from copperchat.kicad_tools.footprints.checks import DEFAULT_TOLERANCE
from copperchat.kicad_tools.footprints.read import (
    DEFAULT_LIST_LIMIT,
    MAX_ALLOW_UNMATCHED,
    MAX_COORD_MM,
    MAX_DRILL_MM,
    MAX_EXPECTED_PADS,
    MAX_LIB_ID_CHARS,
    MAX_LIST_LIMIT,
    MAX_PAD_NUMBER_CHARS,
    MAX_QUERY_CHARS,
    MAX_SIZE_MM,
    MAX_SYMBOL_CHARS,
    MAX_TOLERANCE_MM,
    MIN_TOLERANCE_MM,
    fp_check,
    fp_get,
    fp_list_local,
)
from copperchat.kicad_tools.footprints.render import fp_render
from copperchat.tools.spec import ToolSpec
from copperchat.views.footprint_render import GRID_LADDER, LAYER_RE, MAX_LAYERS


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


_LIB_ID = {
    "type": "string",
    "minLength": 3,
    "maxLength": MAX_LIB_ID_CHARS,
    "description": "<library nickname>:<footprint name>, e.g. 'Inductor_SMD:L_Bourns-SRN4018'.",
}

_PAD_NUMBER = {"type": "string", "minLength": 1, "maxLength": MAX_PAD_NUMBER_CHARS}


def _size(description: str) -> dict[str, Any]:
    return {
        "type": "number",
        "exclusiveMinimum": 0,
        "maximum": MAX_SIZE_MM,
        "description": description,
    }


def _xy(item: dict[str, Any], description: str) -> dict[str, Any]:
    return {
        "type": "array",
        "items": item,
        "minItems": 2,
        "maxItems": 2,
        "description": description,
    }


_COORD = {"type": "number", "minimum": -MAX_COORD_MM, "maximum": MAX_COORD_MM}
_SIZE_ITEM = {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_SIZE_MM}

_EXPECTED_PAD = _obj(
    {
        "number": _PAD_NUMBER,
        "at": _xy(_COORD, "Pad centre [x, y] in mm, footprint origin, y down."),
        "size": _xy(_SIZE_ITEM, "Pad size [w, h] in mm, in board axes."),
        "drill": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": MAX_DRILL_MM,
            "description": "Drill diameter in mm (THT pads).",
        },
    },
    ["number", "at", "size"],
)

_EXPECTED = _obj(
    {
        "pads": {
            "type": "array",
            "items": _EXPECTED_PAD,
            "maxItems": MAX_EXPECTED_PADS,
            "description": "The datasheet's land pattern, one entry per pad.",
        },
        "pitch": _size("Pad pitch in mm."),
        "courtyard": _xy(_SIZE_ITEM, "Courtyard size [w, h] in mm."),
        "body": _xy(_SIZE_ITEM, "Body (F.Fab outline) size [w, h] in mm."),
        "tolerance_mm": {
            "type": "number",
            "minimum": MIN_TOLERANCE_MM,
            "maximum": MAX_TOLERANCE_MM,
            "default": DEFAULT_TOLERANCE,
            "description": f"Allowed deviation in mm (default {DEFAULT_TOLERANCE}).",
        },
    },
    ["pads"],
)

TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        "fp_get",
        "One footprint's geometry by lib_id, from any footprint library (the repo's local "
        "library, project or stock): pads (number, type, shape, position, size, drill, "
        "layers), per-layer graphics summary, texts, 3D models, bbox (pads + shapes, mm, "
        "y down), mounting, fields and geometry_hash; for local footprints also `source` "
        "and `review` (needs_user_review / approved / unreviewed). detail='full' adds the "
        "shape list. Large footprints are capped (pads_truncated / truncated, exact "
        "pad_count). The first footprint call of a project without a board "
        "loads the footprint libraries (several seconds, see notes).",
        _obj(
            {
                "lib_id": _LIB_ID,
                "detail": {
                    "type": "string",
                    "enum": ["summary", "full"],
                    "description": "'summary' (default, no shape list) or 'full'.",
                },
            },
            ["lib_id"],
        ),
        fp_get,
    ),
    ToolSpec(
        "fp_list_local",
        "List the footprints of this repository's local footprint library (the one "
        "CopperChat creates footprints in): name, lib_id, description, pad count, review "
        "state (needs_user_review: unreviewed / needs_user_review / approved), source and "
        "the references of this project's symbols that use it (used_by). library is null "
        "while the project has no local library yet.",
        _obj(
            {
                "query": {
                    "type": "string",
                    "maxLength": MAX_QUERY_CHARS,
                    "description": "Only footprints whose name or description contain every word.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LIST_LIMIT,
                    "default": DEFAULT_LIST_LIMIT,
                    "description": f"Maximum number of footprints (default {DEFAULT_LIST_LIMIT}).",
                },
            },
            [],
        ),
        fp_list_local,
    ),
    ToolSpec(
        "fp_check",
        "Check one footprint (local, project or stock) against KiCad-library-style rules: "
        "courtyard, fab and silk outlines, pad sizes and gaps, silk on pads, drills and "
        "annular rings, mask/paste layers, pin 1 marking, fields and review state. "
        "With `symbol` (a library symbol id like 'Device:L' or a placed reference like "
        "'L201') the symbol's pin numbers are matched against the pad numbers; with "
        "`expected` (the datasheet's land pattern) every pad position/size, the pitch, "
        "courtyard and body are compared within tolerance_mm. Returns passed (no "
        "errors), exact counts and findings (rule, severity, message, pads, at, "
        "measured, expected; capped per rule). Read-only.",
        _obj(
            {
                "lib_id": _LIB_ID,
                "symbol": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": MAX_SYMBOL_CHARS,
                    "description": "Pins to compare: a library symbol id '<nickname>:<name>' "
                    "or the reference of a placed symbol (e.g. 'U202'); all units.",
                },
                "expected": _EXPECTED,
                "allow_unmatched_pads": {
                    "type": "array",
                    "items": _PAD_NUMBER,
                    "maxItems": MAX_ALLOW_UNMATCHED,
                    "description": "Pad numbers without a symbol pin that are intended "
                    "(e.g. shell pads); their warning becomes info.",
                },
            },
            ["lib_id"],
        ),
        fp_check,
    ),
    ToolSpec(
        "fp_render",
        "Render one footprint (local, project or stock) as an image you can look at: "
        "KiCad's drawing of the chosen layers on a mm grid with axes through the origin "
        "(anchor), mm ticks and, with labels, pad 1 size/position, pitch and courtyard "
        "size. Returns the PNG (shown to you and in the chat), image/svg filenames, "
        "svg_url (a same-origin link to the zoomable SVG; give it to the user as a "
        "markdown link), scale_px_per_mm, extent_mm and origin_mm (PNG pixel (px, py) = "
        "footprint mm origin_mm + (px, py) / scale_px_per_mm, y down), the grid drawn "
        "and geometry_hash. Read-only.",
        _obj(
            {
                "lib_id": _LIB_ID,
                "layers": {
                    "type": "array",
                    "items": {"type": "string", "pattern": LAYER_RE.pattern},
                    "minItems": 1,
                    "maxItems": MAX_LAYERS,
                    "description": "Layers to draw, e.g. ['F.Cu', 'F.CrtYd']; default "
                    "F.Cu, F.Mask, F.Paste, F.SilkS, F.Fab, F.CrtYd plus the B.* layers "
                    "the footprint uses.",
                },
                "grid_mm": {
                    "type": "number",
                    "enum": list(GRID_LADDER),
                    "description": "Minor grid in mm (default: automatic from the size).",
                },
                "labels": {
                    "type": "boolean",
                    "default": True,
                    "description": "Draw the dimension labels (default true).",
                },
            },
            ["lib_id"],
        ),
        fp_render,
    ),
]
