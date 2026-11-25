"""Specs of the board checks and approvals (P3b), appended to `board/specs.py`'s
`TOOL_SPECS`: `pcb_check` (read tool, cost 0, never the edit lock)."""

from __future__ import annotations

from copperchat.kicad_tools.board.check import LEVELS, MAX_LISTED, pcb_check
from copperchat.tools.spec import ToolSpec

PCB_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "level": {
            "type": "string",
            "enum": list(LEVELS),
            "description": "quick = KiCad's DRC + metrics M1-M14 (M14 n/a); full = also "
            "schematic parity (kicad-cli) and M14 against the fab preset.",
        },
    },
    "required": ["level"],
    "additionalProperties": False,
}

PCB_CHECK_DESCRIPTION = (
    "Check the board and record the result as evidence for pcb_approve: 'quick' after "
    "placement (KiCad's DRC + metrics), 'full' before the board approval (+ schematic "
    "parity and JLCPCB fab rules M14; takes longer). Result: level, errors and warnings "
    f"from KiCad's DRC (each warning with accepted: true when a board.md Checks row covers "
    f"it; at most {MAX_LISTED} each + *_omitted), parity (full only; null for quick), the "
    "metrics (id, value, verdict, offenders, ...), hard_failures, reviews, verdict (pass | "
    "review | fail), placement_hash, board_hash, stale (true: the board changed while it "
    "ran, nothing recorded — check again), seconds; full adds cli_crosscheck. No board → "
    "no_board; the full check failing to run → drc_rejected. Read-only."
)

CHECK_SPECS: list[ToolSpec] = [
    ToolSpec("pcb_check", PCB_CHECK_DESCRIPTION, PCB_CHECK_SCHEMA, pcb_check),
]
