"""Specs of the board checks and approvals (P3b), appended to `board/specs.py`'s
`TOOL_SPECS`: `pcb_check` (read tool, cost 0, never the edit lock) and `pcb_approve`
(two-phase, cost 1, plan-exempt; writes only database rows)."""

from __future__ import annotations

from copperchat.kicad_tools.board.approve import (
    MAX_QUOTE_CHARS,
    apply_approve,
    prepare_approve,
)
from copperchat.kicad_tools.board.check import LEVELS, MAX_LISTED, pcb_check
from copperchat.kicad_tools.board.checkpoints import STAGES
from copperchat.tools.spec import ToolSpec, edit_spec

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

PCB_APPROVE_SCHEMA = {
    "type": "object",
    "properties": {
        "stage": {
            "type": "string",
            "enum": list(STAGES),
            "description": "placement (before routing) or board (before the fab export).",
        },
        "hash": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
            "description": "The stage's hash of the state the user saw: placement_hash for "
            "placement, board_hash for board.",
        },
        "user_quote": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_QUOTE_CHARS,
            "description": "The user's own words of approval, copied verbatim from their "
            "message of this turn.",
        },
    },
    "required": ["stage", "hash", "user_quote"],
    "additionalProperties": False,
}

PCB_APPROVE_DESCRIPTION = (
    "Record the user's approval of the placement or of the final board. Only after you "
    "showed the evidence and ended the turn: placement = pcb_render view=placement + "
    "pcb_check (M1 passing) of this placement_hash; board = pcb_check full without hard "
    "failures + pcb_render top and bottom of this board_hash. Then, in the turn started by "
    "the user's answer, call it with the stage's hash and the user's words (at least 2 words "
    "/ 8 characters, or their whole message). Refused (approval_not_allowed, details.guard "
    "1-7) for another hash, missing or failing evidence, a board change after the evidence, "
    "an approval in the same turn, a quote the user did not write, or a running plan. Any "
    "later change of the stage's hash invalidates the approval; routing needs an approved "
    "placement, the fab export an approved board. Cost 1; writes no KiCad data."
)

CHECK_SPECS: list[ToolSpec] = [
    ToolSpec("pcb_check", PCB_CHECK_DESCRIPTION, PCB_CHECK_SCHEMA, pcb_check),
    edit_spec(
        "pcb_approve",
        PCB_APPROVE_DESCRIPTION,
        PCB_APPROVE_SCHEMA,
        prepare_approve,
        apply_approve,
    ),
]
