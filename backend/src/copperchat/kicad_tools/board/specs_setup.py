"""Tool specs of the board setup phase (P2): `pcb_sync_from_schematic`, `pcb_set_outline`,
`pcb_set_rules`. Every schema is bounded (plan call 6): `additionalProperties: false` at
every level, every list has `maxItems`, every number finite bounds."""

from __future__ import annotations

from typing import Any

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

SETUP_SPECS: list[ToolSpec] = [
    edit_spec(
        "pcb_sync_from_schematic",
        PCB_SYNC_DESCRIPTION,
        PCB_SYNC_SCHEMA,
        prepare_sync,
        apply_sync,
    ),
]
