"""Tool provider: footprint tools (`fp_*`, footprints spec §7)."""

from __future__ import annotations

from typing import Any

from copperchat.kicad_tools.footprints import build as b
from copperchat.kicad_tools.footprints.checks import DEFAULT_TOLERANCE
from copperchat.kicad_tools.footprints.create import apply_footprint_create, prepare_create
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
from copperchat.tools.spec import ApplyFn, ToolSpec, edit_spec
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

# -- fp_create (spec §7.6) ---------------------------------------------------------------


def _vec3(description: str, item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": item, "minItems": 3, "maxItems": 3,
            "description": description}  # fmt: skip


_RECT = _obj(
    {
        "rect": _xy(_SIZE_ITEM, "[w, h] in mm."),
        "center": _xy(_COORD, "Centre [x, y] in mm (default [0, 0], the anchor)."),
    },
    ["rect"],
)

_CREATE_PAD = _obj(
    {
        "number": {"type": "string", "maxLength": MAX_PAD_NUMBER_CHARS,
                   "description": "Pad number ('' only for NPTH holes)."},
        "type": {"type": "string", "enum": list(b.PAD_TYPES)},
        "shape": {"type": "string", "enum": list(b.PAD_SHAPES)},
        "at": _xy(_COORD, "Pad centre [x, y] in mm, footprint origin, y down."),
        "rotation": {"type": "number", "minimum": -b.MAX_ANGLE_DEG,
                     "maximum": b.MAX_ANGLE_DEG, "description": "Degrees (default 0)."},
        "size": _xy(_SIZE_ITEM, "Pad size [w, h] in mm (pad axes, before rotation)."),
        "roundrect_ratio": {"type": "number", "minimum": 0,
                            "maximum": b.MAX_ROUNDRECT_RATIO,
                            "description": "Corner radius / min(w, h) of a roundrect pad "
                            f"(default {b.DEFAULT_ROUNDRECT_RATIO})."},
        "drill": _obj(
            {
                "size": _xy({"type": "number", "exclusiveMinimum": 0,
                             "maximum": MAX_DRILL_MM}, "Drill [w, h] in mm."),
                "shape": {"type": "string", "enum": list(b.DRILL_SHAPES)},
            },
            ["size"],
        ),
        "layers": {
            "type": "array",
            "items": {"type": "string", "pattern": b.PAD_LAYER_RE.pattern},
            "minItems": 1,
            "maxItems": b.MAX_PAD_LAYERS,
            "uniqueItems": True,
            "description": "Default by type: smd F.Cu F.Mask F.Paste; tht and npth *.Cu "
            "*.Mask; connector F.Cu F.Mask.",
        },
    },
    ["number", "type", "shape", "at", "size"],
)  # fmt: skip

_SEGMENT = {
    "type": "array",
    "items": _xy(_COORD, "[x, y] in mm."),
    "minItems": 2,
    "maxItems": 2,
}

FP_CREATE_SCHEMA = _obj(
    {
        "name": {"type": "string", "minLength": 1, "maxLength": b.MAX_NAME_CHARS,
                 "pattern": "^[A-Za-z0-9][A-Za-z0-9_.+-]{0,99}$",
                 "description": "New footprint name, KiCad style, e.g. L_Bourns_SRN3012BTA."},
        "description": {"type": "string", "maxLength": b.MAX_DESCRIPTION_CHARS},
        "keywords": {"type": "string", "maxLength": b.MAX_KEYWORDS_CHARS},
        "mounting": {"type": "string", "enum": list(b.MOUNTINGS)},
        "pads": {"type": "array", "items": _CREATE_PAD, "minItems": 1,
                 "maxItems": b.MAX_PADS},
        "courtyard": _obj(
            {
                "rect": _xy(_SIZE_ITEM, "Courtyard [w, h] in mm."),
                "center": _xy(_COORD, "Courtyard centre [x, y] in mm."),
                "margin": {"type": "number", "minimum": 0, "maximum": b.MAX_MARGIN_MM,
                           "description": f"Gap around pads + body (default "
                           f"{b.DEFAULT_MARGIN}), rounded outward to 0.01 mm."},
            },
            [],
        ),
        "body": _RECT,
        "silkscreen": {
            "anyOf": [
                {"type": "string", "enum": ["auto"]},
                _obj({"segments": {"type": "array", "items": _SEGMENT,
                                   "maxItems": b.MAX_SILK_SEGMENTS}}, ["segments"]),
            ],
            "description": "'auto' (default): the body outline, clipped 0.2 mm clear of "
            "copper; or explicit F.SilkS segments.",
        },
        "pin1_marker": {"type": "string", "enum": ["auto", "none"],
                        "description": "'auto' (default): a silk mark beside pad 1 and "
                        "the body corner nearest pad 1 chamfered on F.Fab."},
        "fields": {
            "type": "object",
            "maxProperties": b.MAX_EXTRA_FIELDS,
            "propertyNames": {"maxLength": b.MAX_FIELD_NAME_CHARS},
            "additionalProperties": {"type": "string",
                                     "maxLength": b.MAX_FIELD_VALUE_CHARS},
            "description": "Extra fields, e.g. Datasheet, Manufacturer. Reference, Value, "
            "Footprint source, needs_user_review and User review are set by the tool.",
        },
        "model": _obj(
            {
                "path": {"type": "string", "minLength": 1,
                         "maxLength": b.MAX_MODEL_PATH_CHARS,
                         "description": "${KICAD<n>_3DMODEL_DIR}/... (n 6..11) or "
                         "${KIPRJMOD}/...; .step/.stp/.wrl/.wrz; a link only."},
                "offset": _vec3("[x, y, z] in mm.", _COORD),
                "rotation": _vec3("[x, y, z] in degrees.",
                                  {"type": "number", "minimum": -b.MAX_ANGLE_DEG,
                                   "maximum": b.MAX_ANGLE_DEG}),
                "scale": _vec3("[x, y, z] factors.",
                               {"type": "number", "exclusiveMinimum": 0,
                                "maximum": b.MAX_MODEL_SCALE}),
            },
            ["path"],
        ),
        "source": _obj(
            {
                "document": {"type": "string", "minLength": 1,
                             "maxLength": b.MAX_DOCUMENT_URL_CHARS,
                             "description": "Repo-relative path of the datasheet (<= "
                             f"{b.MAX_DOCUMENT_PATH_CHARS} chars) or an http(s) URL."},
                "location": {"type": "string", "minLength": 1,
                             "maxLength": b.MAX_LOCATION_CHARS,
                             "description": "Where the land pattern is: page, figure or "
                             "table."},
                "mpn": {"type": "string", "maxLength": b.MAX_MPN_CHARS},
            },
            ["document", "location"],
        ),
        "symbol": {"type": "string", "minLength": 2, "maxLength": MAX_SYMBOL_CHARS,
                   "description": "Symbol whose pins the check compares ('Device:L' or a "
                   "placed reference)."},
        "replace": {"type": "boolean", "default": False,
                    "description": "Rebuild an existing local footprint (needs the user's "
                    "OK; the result needs review again)."},
    },
    sorted(b.REQUIRED_KEYS),
)  # fmt: skip

FP_CREATE_DESCRIPTION = (
    "Create a footprint in this repository's local footprint library from explicit "
    "datasheet geometry (mm, footprint origin, y down): pads (layers default by type), "
    "body (F.Fab outline), optional courtyard (default 0.25 mm around pads + body), "
    "silkscreen and pin-1 marker (auto), fields, an optional 3D model link and a "
    "MANDATORY source (datasheet path or URL + page/figure). The target is checked with "
    "fp_check first (errors refuse the call), built through KiCad and read back. The new "
    "footprint needs the user's review: call fp_render and show the render, the check and "
    "the source. Counts 5 changes against the turn budget."
)


def fp_create_spec(apply: ApplyFn) -> ToolSpec:
    """The two-phase `fp_create` spec with its `apply` (the write engine)."""
    return edit_spec("fp_create", FP_CREATE_DESCRIPTION, FP_CREATE_SCHEMA, prepare_create, apply)


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
    fp_create_spec(apply_footprint_create),
]
