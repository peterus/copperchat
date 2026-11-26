"""Tool provider: board tools (`pcb_*`, SP4 spec §7). Later phases append their specs to
`TOOL_SPECS`; every schema here is bounded (plan call 6, `test_every_board_schema_bounded`)."""

from __future__ import annotations

from typing import Any

from copperchat.kicad_tools.board.specs_setup import SETUP_SPECS
from copperchat.kicad_tools.board.status import pcb_status
from copperchat.kicad_tools.board.views import (
    MAX_COORD_MM,
    MAX_HIGHLIGHT_NETS,
    MAX_LAYER_CHARS,
    MAX_NET_CHARS,
    MAX_REGION_MM,
    pcb_render,
)
from copperchat.tools.spec import ToolSpec
from copperchat.views.board_render import ANGLES, BOARD_VIEWS


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


PCB_STATUS_DESCRIPTION = (
    "Cheap summary of the board; call it at the start of every board turn. Without a board: "
    "{exists: false, suggestion}. Otherwise: outline (kind, x, y, w, h, corner_radius in mm, "
    "null without Edge.Cuts), copper layer count, footprints placed/unplaced (unplaced = "
    "courtyard not fully inside the outline; up to 50 refs listed), connections (pad pairs a "
    "fully routed board has), routed and unrouted connections (KiCad's DRC), copper zone "
    "count, the placement_hash / board_hash of the current state, checkpoints (placement "
    "and board: approved, stale = changed since the approval, hash, approved_at, "
    "conversation_id) and last_check (the newest pcb_check of this session: level, "
    "hard_failures, m1_pass, created_at, for_current = it checked the current state; "
    "null if none). Read-only."
)

_COORD = {"type": "number", "minimum": -MAX_COORD_MM, "maximum": MAX_COORD_MM}
_SIZE = {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_REGION_MM}

PCB_RENDER_SCHEMA = _obj(
    {
        "view": {
            "type": "string",
            "enum": list(BOARD_VIEWS),
            "description": "top (F.Cu, mask, silk, outline), bottom (mirrored), layer (one "
            "copper layer + outline), placement (courtyards, fab, silk, block hulls), 3d.",
        },
        "layer": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_LAYER_CHARS,
            "description": "Copper layer for view 'layer', e.g. 'B.Cu' or 'In1.Cu'.",
        },
        "region": _obj(
            {"x": _COORD, "y": _COORD, "w": _SIZE, "h": _SIZE},
            ["x", "y", "w", "h"],
        )
        | {"description": "Zoom to this board rectangle (mm, x right, y down); 2D views."},
        "highlight_nets": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": MAX_NET_CHARS},
            "maxItems": MAX_HIGHLIGHT_NETS,
            "description": "Nets drawn as thick highlighted strokes (2D views).",
        },
        "markers": {
            "type": "boolean",
            "description": "Run KiCad's DRC and draw its violations as numbered circles; "
            "the numbers are the ids of the returned markers list.",
        },
        "angle": {
            "type": "string",
            "enum": list(ANGLES),
            "description": "Camera of view '3d' (default top; iso = perspective from the "
            "front left). Bare board: no 3D component models yet.",
        },
    },
    ["view"],
)

PCB_RENDER_DESCRIPTION = (
    "Look at the board: one PNG (long side at most 1600 px) plus svg_url, a zoomable SVG "
    "link to show the user (2D views). The result has the placement_hash and board_hash "
    "of the rendered state and, with markers: true, the DRC markers (id, type, severity, "
    "text, x, y; at most 100) drawn as numbered circles. Unknown layer or net → not_found "
    "with the closest names. Read-only."
)

TOOL_SPECS: list[ToolSpec] = [
    ToolSpec("pcb_status", PCB_STATUS_DESCRIPTION, _obj({}, []), pcb_status),
    ToolSpec("pcb_render", PCB_RENDER_DESCRIPTION, PCB_RENDER_SCHEMA, pcb_render),
    *SETUP_SPECS,  # P2
]

# P3a: the analysis read tools (pcb_analyze; pcb_metrics joins ANALYSIS_SPECS)
from copperchat.kicad_tools.board.specs_analysis import ANALYSIS_SPECS  # noqa: E402

TOOL_SPECS += ANALYSIS_SPECS

# P3b: checks and approvals (pcb_check; pcb_approve joins CHECK_SPECS)
from copperchat.kicad_tools.board.specs_check import CHECK_SPECS  # noqa: E402

TOOL_SPECS += CHECK_SPECS

# P4: placement (pcb_define_blocks, pcb_place_block, pcb_move, pcb_legalize)
from copperchat.kicad_tools.board.specs_place import PLACE_SPECS  # noqa: E402

TOOL_SPECS += PLACE_SPECS

# P5b: own router (pcb_route, pcb_unroute)
from copperchat.kicad_tools.board.specs_route import ROUTE_SPECS  # noqa: E402

TOOL_SPECS += ROUTE_SPECS

# P6b: Freerouting fill (pcb_route_fill)
from copperchat.kicad_tools.board.specs_fill import FILL_SPECS  # noqa: E402

TOOL_SPECS += FILL_SPECS

# P7: copper (pcb_zones, pcb_fanout, pcb_stitch, pcb_tidy_silk)
from copperchat.kicad_tools.board.specs_copper import COPPER_SPECS  # noqa: E402

TOOL_SPECS += COPPER_SPECS

# P8: fab exports (pcb_fab_export, pcb_export_3d)
from copperchat.kicad_tools.board.specs_fab import FAB_SPECS  # noqa: E402

TOOL_SPECS += FAB_SPECS
