"""Tool specs of the board setup phase (P2): `pcb_sync_from_schematic`, `pcb_set_outline`,
`pcb_set_rules`. Every schema is bounded (plan call 6): `additionalProperties: false` at
every level, every list has `maxItems`, every number finite bounds."""

from __future__ import annotations

from typing import Any

from copperchat.kicad_tools.board.setup import (
    apply_outline,
    apply_rules,
    prepare_outline,
    prepare_rules,
)
from copperchat.kicad_tools.board.sync import apply_sync, prepare_sync
from copperchat.tools.spec import ToolSpec, edit_spec

# Names of blocks, zones, keepouts and rules (plan call 6).
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 _.+-]{0,63}$"
MAX_COORD_MM = 1000.0


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


# -- pcb_sync_from_schematic -------------------------------------------------------------

PCB_SYNC_SCHEMA = _obj(
    {
        "delete_extra": {
            "type": "boolean",
            "description": "Remove board footprints whose symbol is gone from the schematic "
            "(default true). Mounting holes placed by pcb_set_outline are never removed.",
        },
        "dry_run": {
            "type": "boolean",
            "description": "Only report what the sync would change; nothing is written and "
            "the call costs no budget (default false).",
        },
    },
    [],
)

PCB_SYNC_DESCRIPTION = (
    "Bring the schematic into the board: creates the board when the project has none, "
    "imports the saved schematic's netlist (new footprints, changed values/footprints, "
    "nets) and puts every new footprint into a staging area right of the outline (not "
    "placed yet). Existing placement is kept. Needs a saved schematic (schematic_unsaved "
    "otherwise). Returns added/removed/changed refs, nets_added/nets_removed, KiCad's "
    "warnings (a pad/pin warning names the part and suggests fp_check), footprints, "
    "unplaced and the staging_box; next step pcb_set_outline. A failure after the import "
    "started cannot be reverted item by item: undo the turn from the History panel."
)

# -- pcb_set_outline ---------------------------------------------------------------------


def _coord(what: str) -> dict[str, Any]:
    return {
        "type": "number",
        "minimum": -MAX_COORD_MM,
        "maximum": MAX_COORD_MM,
        "description": what,
    }


def _point(what: str) -> dict[str, Any]:
    p = _obj({"x": _coord("x in mm"), "y": _coord("y in mm (down)")}, ["x", "y"])
    p["description"] = what
    return p


def _size(lo: float, hi: float, what: str) -> dict[str, Any]:
    return {"type": "number", "minimum": lo, "maximum": hi, "description": what}


PCB_SET_OUTLINE_SCHEMA = _obj(
    {
        "shape": {"type": "string", "enum": ["rect"], "description": "Outline shape."},
        "width": _size(5, 500, "Board width in mm."),
        "height": _size(5, 500, "Board height in mm."),
        "corner_radius": _size(
            0, 250, "Corner radius in mm, at most min(width, height) / 2 (default 0)."
        ),
        "origin": _point("Top-left corner of the outline (default {x: 100, y: 100})."),
        "holes": _obj(
            {
                "footprint": {
                    "type": "string",
                    "minLength": 3,
                    "maxLength": 128,
                    "description": "Library id of the hole footprint (default "
                    "MountingHole:MountingHole_2.2mm_M2).",
                },
                "inset": _size(
                    0.5, 50, "Distance of the corner holes from the edges in mm (default 3)."
                ),
                "positions": {
                    "type": "array",
                    "items": _point("Hole centre."),
                    "maxItems": 16,
                    "description": "Hole centres; default the 4 corners inset by `inset`.",
                },
            },
            [],
        ),
        "keepouts": {
            "type": "array",
            "maxItems": 16,
            "description": "Keepout rule areas; an existing rule area with the same name is "
            "replaced, other rule areas are kept.",
            "items": _obj(
                {
                    "name": {"type": "string", "pattern": NAME_PATTERN},
                    "polygon": {
                        "type": "array",
                        "items": _point("Polygon corner."),
                        "minItems": 3,
                        "maxItems": 256,
                        "description": "The area (give polygon or rect).",
                    },
                    "rect": _obj(
                        {
                            "x": _coord("left in mm"),
                            "y": _coord("top in mm"),
                            "w": {"type": "number", "exclusiveMinimum": 0, "maximum": 500},
                            "h": {"type": "number", "exclusiveMinimum": 0, "maximum": 500},
                        },
                        ["x", "y", "w", "h"],
                    ),
                    "layers": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 32},
                        "minItems": 1,
                        "maxItems": 32,
                        "description": 'Enabled board layers (e.g. "F.Cu") or "all_copper".',
                    },
                    "no": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["tracks", "vias", "copper", "footprints"],
                        },
                        "minItems": 1,
                        "maxItems": 4,
                        "uniqueItems": True,
                        "description": "What the area keeps out (copper = zone fills).",
                    },
                },
                ["name", "layers", "no"],
            ),
        },
    },
    ["shape", "width", "height"],
)

PCB_SET_OUTLINE_DESCRIPTION = (
    "Give the board its outline: replaces every Edge.Cuts shape with one rectangle "
    "(optional rounded corners; origin = top-left corner). With `holes`, the schematic's "
    "mounting holes (MountingHole footprints with a symbol) are moved to the hole positions "
    "(default the 4 corners inset by `inset`) and locked; holes placed by an earlier call "
    "are replaced, and the remaining positions get new locked holes that are not in the "
    "schematic and excluded from BOM and position files. Without `holes` no hole is "
    "touched; symbol-less holes the user placed by hand are never removed (listed in "
    "holes.kept). `keepouts` creates rule areas (same name = replaced). Returns outline, "
    "holes {moved, placed, removed, kept}, keepouts, unplaced and the placement_hash change; an "
    "unknown hole footprint is not_found with the closest ids. Next step pcb_set_rules."
)

# -- pcb_set_rules ---------------------------------------------------------------------

_MM = {"type": "number", "exclusiveMinimum": 0, "maximum": 100}
_DRILL = {"type": "number", "exclusiveMinimum": 0, "maximum": 10}
_OVERRIDE = {"type": "number", "exclusiveMinimum": 0, "maximum": 10}
_LIMIT = {"type": "number", "minimum": 0, "maximum": 100}
_NAME = {"type": "string", "pattern": NAME_PATTERN}

PCB_SET_RULES_SCHEMA = _obj(
    {
        "layers": {
            "type": "integer",
            "enum": [2, 4, 6],
            "minimum": 2,
            "maximum": 6,
            "description": "Copper layer count. Only this changes it (a preset never does). "
            "Going down is refused while tracks, zones or graphics sit on a removed layer.",
        },
        "preset": {
            "type": "string",
            "enum": ["jlcpcb-2l", "jlcpcb-4l", "jlcpcb-6l"],
            "description": "Fab preset: sets clearance, track width, via size, drill, "
            "annular ring, copper-to-edge, hole-to-hole and silk text minimums.",
        },
        "overrides": _obj(
            {
                k: _OVERRIDE
                for k in (
                    "min_space",
                    "min_track",
                    "min_via_diameter",
                    "min_drill",
                    "min_annular",
                    "copper_to_edge",
                    "hole_to_hole",
                    "silk_min_height",
                    "silk_min_line",
                )
            },
            [],
        )
        | {
            "description": "Tighter minimums than the preset (mm). A value below the preset "
            "is refused: an exception needs custom_rules with a reason."
        },
        "net_classes": {
            "type": "array",
            "maxItems": 16,
            "description": "Net classes (merged: classes not named are kept; Default only "
            "changes when given). patterns are net name globs and replace the class's "
            "earlier patterns.",
            "items": _obj(
                {
                    "name": _NAME,
                    "track_width": _MM,
                    "clearance": _MM,
                    "via_diameter": _MM,
                    "via_drill": _DRILL,
                    "diff_pair_width": _MM,
                    "diff_pair_gap": _MM,
                    "patterns": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 50,
                        "items": {"type": "string", "minLength": 1, "maxLength": 256},
                    },
                },
                ["name", "track_width", "clearance", "via_diameter", "via_drill", "patterns"],
            ),
        },
        "custom_rules": {
            "type": "array",
            "maxItems": 32,
            "description": "KiCad custom rules (same name = replaced), each with the reason "
            "for the exception (written as the rule's comment). min and/or max in mm.",
            "items": _obj(
                {
                    "name": _NAME,
                    "condition": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 512,
                        "description": "KiCad rule condition, e.g. A.NetClass == 'Power'",
                    },
                    "constraint": {
                        "type": "string",
                        "enum": [
                            "clearance",
                            "track_width",
                            "via_diameter",
                            "hole_size",
                            "hole_to_hole",
                            "hole_clearance",
                            "edge_clearance",
                            "annular_width",
                            "courtyard_clearance",
                            "silk_clearance",
                            "text_height",
                            "diff_pair_gap",
                        ],
                    },
                    "min": _LIMIT,
                    "max": _LIMIT,
                    "reason": {"type": "string", "minLength": 8, "maxLength": 300},
                },
                ["name", "condition", "constraint", "reason"],
            ),
        },
        "stackup": {
            "type": "string",
            "maxLength": 64,
            "description": "Stackup preset id; not available on this server yet "
            "(stackup_unavailable).",
        },
    },
    [],
)

PCB_SET_RULES_DESCRIPTION = (
    "Set up the board's rules: copper layer count (`layers`, the only way to change it), "
    "the JLCPCB fab preset's minimum rules (`preset`, plus tighter `overrides`), net "
    "classes with net name patterns (a pattern belongs to one class: giving it to a class "
    "takes it from its previous one, listed in patterns_moved), and custom rules for "
    "reasoned exceptions (each with "
    "a `reason`). At least one field. Every part is undone if a later one fails. Returns "
    "the rules table, the net classes, the custom rules with their reasons, notes (e.g. a "
    "preset for another layer count), the board_hash change; next step pcb_analyze. "
    "`stackup` is not available yet (stackup_unavailable)."
)

SETUP_SPECS: list[ToolSpec] = [
    edit_spec(
        "pcb_sync_from_schematic",
        PCB_SYNC_DESCRIPTION,
        PCB_SYNC_SCHEMA,
        prepare_sync,
        apply_sync,
    ),
    edit_spec(
        "pcb_set_outline",
        PCB_SET_OUTLINE_DESCRIPTION,
        PCB_SET_OUTLINE_SCHEMA,
        prepare_outline,
        apply_outline,
    ),
    edit_spec(
        "pcb_set_rules",
        PCB_SET_RULES_DESCRIPTION,
        PCB_SET_RULES_SCHEMA,
        prepare_rules,
        apply_rules,
    ),
]
