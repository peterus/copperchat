"""Specs of the placement tools (P4), appended to `board/specs.py`'s `TOOL_SPECS`:
`pcb_define_blocks`, `pcb_place_block`, `pcb_move`, `pcb_legalize` (all two-phase board
writes); R2 adds `pcb_floorplan` (read) and `pcb_place_blocks` (write). Every schema is bounded (plan call 6): `additionalProperties: false` at every
level, every list `maxItems`, every number finite bounds."""

from __future__ import annotations

from typing import Any

from copperchat.board.placer.floorplan import (
    CHANNEL_MAX_MM,
    CHANNEL_MIN_MM,
    CHANNEL_PER_NET_MM,
    PACK_MAX,
    SPREAD_SPAN_MIN,
)
from copperchat.kicad_tools.board.place import (
    DEFAULT_KEEP_CHANNELS_MM,
    EDGES,
    MAX_BLOCK_REFS,
    MAX_BLOCKS,
    MAX_COORD_MM,
    MAX_ITEMS,
    MAX_KEEP_CHANNELS_MM,
    MAX_OFFSET_MM,
    MAX_REF_CHARS,
    MAX_REGION_MM,
    MAX_ROT_DEG,
    MAX_SHIFT_MM,
    ROTATIONS,
    SIDES,
    apply_define_blocks,
    apply_placement,
    pcb_floorplan,
    prepare_define_blocks,
    prepare_legalize,
    prepare_move,
    prepare_place_block,
    prepare_place_blocks,
)
from copperchat.kicad_tools.board.specs_setup import NAME_PATTERN
from copperchat.tools.spec import ToolSpec, edit_spec


def _obj(props: dict[str, Any], required: list[str], what: str = "") -> dict[str, Any]:
    o: dict[str, Any] = {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }
    if what:
        o["description"] = what
    return o


def _ref(what: str = "Reference designator, e.g. 'U1'.") -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": MAX_REF_CHARS, "description": what}


def _coord(what: str) -> dict[str, Any]:
    return {
        "type": "number",
        "minimum": -MAX_COORD_MM,
        "maximum": MAX_COORD_MM,
        "description": what,
    }


_ROT = {
    "type": "number",
    "minimum": -MAX_ROT_DEG,
    "maximum": MAX_ROT_DEG,
    "description": "Rotation in degrees (counter-clockwise).",
}
_SIDE = {"type": "string", "enum": list(SIDES), "description": "Board side."}
_BLOCK = {
    "type": "string",
    "pattern": NAME_PATTERN,
    "description": "Block name (the group is blk:<name>).",
}
_UNROUTE = {
    "type": "boolean",
    "description": "Allow moving parts with tracks/vias attached: all copper of their "
    "connections is removed first (locked routes and vias included) and reported as "
    "unrouted. Default false: such parts are refused with part_routed.",
}

# -- pcb_define_blocks ---------------------------------------------------------------------

PCB_DEFINE_BLOCKS_SCHEMA = _obj(
    {
        "blocks": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_BLOCKS,
            "items": _obj(
                {
                    "name": _BLOCK,
                    "refs": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_BLOCK_REFS,
                        "items": _ref(),
                        "description": "The parts of the block; a part belongs to one block.",
                    },
                    "anchor": _ref("The block's main part (one of refs), e.g. its IC."),
                },
                ["name", "refs"],
            ),
            "description": "Functional blocks, e.g. from pcb_analyze's block proposals.",
        },
        "replace": {
            "type": "boolean",
            "description": "true: remove every other blk:* group; false (default): replace "
            "only same-named blocks and keep the others.",
        },
    },
    ["blocks"],
)

PCB_DEFINE_BLOCKS_DESCRIPTION = (
    "Store functional blocks as KiCad groups blk:<name> (one group per part: a part leaves "
    "its previous block). Start from pcb_analyze's block proposals; mounting holes and "
    "fiducials cannot be in a block. Unknown refs → not_found with details.closest (ref → "
    "closest refs). Returns the board's blocks with their part counts and the refs not in "
    "any block. Cost 1. Next: pcb_place_block for each block, largest first."
)

# -- pcb_place_block -----------------------------------------------------------------------

PCB_PLACE_BLOCK_SCHEMA = _obj(
    {
        "block": _BLOCK,
        "region": _obj(
            {
                "x": _coord("Region left (mm)."),
                "y": _coord("Region top (mm, y down)."),
                "w": {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_REGION_MM},
                "h": {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_REGION_MM},
                "edge": {
                    "type": "string",
                    "enum": list(EDGES),
                    "description": "Instead of x/y/w/h: a strip along this outline edge, "
                    "as deep as the block.",
                },
                "offset": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": MAX_OFFSET_MM,
                    "description": "With edge: where along the edge (mm from its top/left "
                    "end) the block centre goes; default the middle.",
                },
            },
            [],
            "Either {x, y, w, h} or {edge, offset?}.",
        ),
        "side": {**_SIDE, "description": "Board side of the block (default top)."},
        "rotation": {
            "type": "integer",
            "enum": list(ROTATIONS),
            "minimum": 0,
            "maximum": 270,
            "description": "Fixed block rotation; default: the best of 0/90/180/270.",
        },
        "keep_channels": {
            "type": "number",
            "minimum": 0,
            "maximum": MAX_KEEP_CHANNELS_MM,
            "description": f"Routing gap (mm) around the block and between its clusters "
            f"(default {DEFAULT_KEEP_CHANNELS_MM}).",
        },
        "fixed": {
            "type": "array",
            "maxItems": MAX_ITEMS,
            "items": _obj(
                {
                    "ref": _ref(),
                    "x": _coord("x (mm)."),
                    "y": _coord("y (mm)."),
                    "rot": _ROT,
                    "side": _SIDE,
                },
                ["ref", "x", "y", "rot", "side"],
            ),
            "description": "Parts put exactly here first (e.g. a connector at its edge); "
            "the rest of the block packs around them.",
        },
        "unroute": _UNROUTE,
    },
    ["block", "region"],
)

PCB_PLACE_BLOCK_DESCRIPTION = (
    "Place one block (group blk:<block>) in one step: its parts are clustered around their "
    "anchors (decoupling caps at their pins, crystal, switcher passives), packed into the "
    "region on a 0.5 mm grid (connectors pulled to their board.md edge) and legalised "
    "against everything already placed. SMD parts only on board.md's allowed sides "
    "(invalid_argument otherwise). Refused without writing anything: unknown_block "
    "(closest names), does_not_fit (details needed_mm2, available_mm2, refs), part_routed. "
    "Returns moved refs, the region used, unrouted nets, placement_hash before/after, the "
    "placement score before/after (M1 violations, M8 worst mm, M9 reviews, M10 worst "
    "ratio, airwire mm, max pins per 5x5 mm cell) and next (pcb_place_block for blocks "
    "still unplaced, else pcb_metrics + pcb_render view placement). Cost 1."
)

# -- pcb_move ------------------------------------------------------------------------------

PCB_MOVE_SCHEMA = _obj(
    {
        "items": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_ITEMS,
            "items": _obj(
                {
                    "ref": _ref(),
                    "x": _coord("New x (mm) of the footprint origin."),
                    "y": _coord("New y (mm, down) of the footprint origin."),
                    "rot": _ROT,
                    "side": _SIDE,
                    "locked": {"type": "boolean", "description": "Set or clear KiCad's lock."},
                },
                ["ref"],
            ),
            "description": "Exact moves; each needs ref plus at least one other field "
            "(unset fields keep the part's value).",
        },
        "legalize": {
            "type": "boolean",
            "description": "Afterwards push the moved parts apart from overlaps and into the "
            "outline (default false).",
        },
        "unroute": _UNROUTE,
    },
    ["items"],
)

PCB_MOVE_DESCRIPTION = (
    "Move, rotate, flip or (un)lock parts exactly, e.g. a connector onto the user's edge or "
    "a decoupling cap closer to its pin, in one commit. Side changes are KiCad's left/right "
    "flip; SMD parts only on board.md's allowed sides. Unknown refs → not_found (closest "
    "refs). Returns moved refs, unrouted nets, placement_hash before/after (any move "
    "invalidates an approved placement), the placement score before/after and next. Cost "
    "ceil(items / 10)."
)

# -- pcb_legalize --------------------------------------------------------------------------

PCB_LEGALIZE_SCHEMA = _obj(
    {
        "scope": {
            "type": "string",
            "enum": ["all", "block"],
            "description": "all = every unlocked part on the board; block = one block.",
        },
        "block": _BLOCK,
        "max_shift": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": MAX_SHIFT_MM,
            "description": "Refuse (does_not_fit) instead of moving any part further (mm).",
        },
        "unroute": _UNROUTE,
    },
    ["scope"],
)

PCB_LEGALIZE_DESCRIPTION = (
    "Remove courtyard overlaps and outline / keepout violations with the smallest shifts "
    "(0.05 mm grid); locked parts and parts outside the outline never move. Refused "
    "without writing: does_not_fit (no legal result, or a shift over max_shift), "
    "part_routed. Returns moved refs (empty when already legal: then nothing is written "
    "and it costs nothing), placement_hash before/after, the placement score before/after "
    "and next. Cost 1."
)

# -- pcb_floorplan / pcb_place_blocks (R2) ---------------------------------------------------

_FLOORPLAN_PROPS: dict[str, Any] = {
    "blocks": {
        "type": "array",
        "minItems": 1,
        "maxItems": MAX_BLOCKS,
        "items": _BLOCK,
        "description": "Block names (pcb_define_blocks); default every defined block. Parts "
        "of other blocks and locked parts stay where they are.",
    },
    "keep_channels": {
        "type": "number",
        "minimum": 0,
        "maximum": MAX_KEEP_CHANNELS_MM,
        "description": f"Routing gap (mm) between a block's clusters and the least channel "
        f"between blocks (default {DEFAULT_KEEP_CHANNELS_MM}).",
    },
}

PCB_FLOORPLAN_SCHEMA = _obj(dict(_FLOORPLAN_PROPS), [])

_FLOORPLAN_TEXT = (
    "Blocks with a board.md Placement edges row go into a strip along that edge "
    "(connector facing it; a number in the row = mm along the edge), a Blocks row with a "
    "Region (x, y, w, h in mm, or left/right/top/bottom half, top-left quarter ...) into "
    "that region; the others split the rest of the board by connectivity (fewest shared "
    "nets across each cut, area balanced), so the whole board is used. Every cut keeps a "
    f"routing channel of {CHANNEL_PER_NET_MM} mm per crossing net "
    f"({CHANNEL_MIN_MM}-{CHANNEL_MAX_MM} mm, at least keep_channels). courtyard_fill = "
    "the parts' courtyards / usable area; does_not_fit (details courtyard_fill, "
    f"needed_mm2, available_mm2, refs) when the blocks need more than {PACK_MAX:.0%} of "
    "the board or their shapes do not tile it: ask the user for a bigger board or the "
    "bottom side."
)

PCB_FLOORPLAN_DESCRIPTION = (
    "Preview where pcb_place_blocks would put every block, without writing anything. "
    + _FLOORPLAN_TEXT
    + " Returns regions [{block, x, y, w, h}], channels [{x1, y1, x2, y2, width}], fixed "
    "blocks, courtyard_fill, usable_mm2 and one placement render with the regions drawn. "
    "Show it to the user before placing. Cost 0."
)

PCB_PLACE_BLOCKS_SCHEMA = _obj({**_FLOORPLAN_PROPS, "unroute": _UNROUTE}, [])

PCB_PLACE_BLOCKS_DESCRIPTION = (
    "Place all blocks (or the given ones) over the whole board in one step: the floorplan "
    "of pcb_floorplan, then each block clustered and packed into its region (as "
    "pcb_place_block), one legalise over everything, and adjacent connected 2-pad parts "
    "aligned (shift <= 1 mm when legal) so a straight connection between their pads is "
    "possible. "
    + _FLOORPLAN_TEXT
    + " Deterministic. Refused without writing: unknown_block, does_not_fit, part_routed. "
    "Returns moved refs, regions, channels, courtyard_fill, spread (how much of the usable "
    f"width/height the parts span; well_spread = spread >= {SPREAD_SPAN_MIN}), "
    "placement_hash before/after, the placement score before/after and next. Cost 1."
)

PLACE_SPECS: list[ToolSpec] = [
    edit_spec(
        "pcb_define_blocks",
        PCB_DEFINE_BLOCKS_DESCRIPTION,
        PCB_DEFINE_BLOCKS_SCHEMA,
        prepare_define_blocks,
        apply_define_blocks,
    ),
    edit_spec(
        "pcb_place_block",
        PCB_PLACE_BLOCK_DESCRIPTION,
        PCB_PLACE_BLOCK_SCHEMA,
        prepare_place_block,
        apply_placement,
    ),
    edit_spec("pcb_move", PCB_MOVE_DESCRIPTION, PCB_MOVE_SCHEMA, prepare_move, apply_placement),
    edit_spec(
        "pcb_legalize",
        PCB_LEGALIZE_DESCRIPTION,
        PCB_LEGALIZE_SCHEMA,
        prepare_legalize,
        apply_placement,
    ),
    ToolSpec("pcb_floorplan", PCB_FLOORPLAN_DESCRIPTION, PCB_FLOORPLAN_SCHEMA, pcb_floorplan),
    edit_spec(
        "pcb_place_blocks",
        PCB_PLACE_BLOCKS_DESCRIPTION,
        PCB_PLACE_BLOCKS_SCHEMA,
        prepare_place_blocks,
        apply_placement,
    ),
]
