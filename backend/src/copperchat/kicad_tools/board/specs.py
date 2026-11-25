"""Tool provider: board tools (`pcb_*`, SP4 spec §7). Later phases append their specs to
`TOOL_SPECS`; every schema here is bounded (plan call 6, `test_every_board_schema_bounded`)."""

from __future__ import annotations

from typing import Any

from copperchat.kicad_tools.board.status import pcb_status
from copperchat.tools.spec import ToolSpec


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
    "count, and the placement_hash / board_hash of the current state. Read-only."
)

TOOL_SPECS: list[ToolSpec] = [
    ToolSpec("pcb_status", PCB_STATUS_DESCRIPTION, _obj({}, []), pcb_status),
]
