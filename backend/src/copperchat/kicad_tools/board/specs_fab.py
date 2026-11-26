"""Specs of the fab exports (P8), appended to `board/specs.py`'s `TOOL_SPECS`: plain
`ToolSpec`s (index call 7: `mutating=True`, `changes_documents=False`, cost 0, no change
entry - they write only below `<project>/fab/`, never KiCad data)."""

from __future__ import annotations

from copperchat.kicad_tools.board.fab import FORMATS, PRESETS, pcb_export_3d, pcb_fab_export
from copperchat.tools.spec import ToolSpec

PCB_FAB_EXPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "preset": {
            "type": "string",
            "enum": list(PRESETS),
            "description": "The fab house: jlcpcb (gerbers, Excellon drill, upload zip, BOM, "
            "CPL in JLCPCB's formats).",
        },
    },
    "required": ["preset"],
    "additionalProperties": False,
}

PCB_FAB_EXPORT_DESCRIPTION = (
    "Write the orderable fab package of the APPROVED board into fab/jlcpcb/: gerbers/ (every "
    "copper layer, mask, silk, paste of both sides for the stencil, Edge.Cuts, .gbrjob), "
    "drill/ (Excellon mm, PTH and NPTH separate, map), <board>-jlcpcb.zip (gerbers + drill: "
    "the upload), bom-jlcpcb.csv, cpl-jlcpcb.csv and fab-report.md (board_hash, metrics, "
    "accepted warnings, custom rules, order notes: thickness, finish, stencil sides). Needs "
    "the board approval of the current board_hash (pcb_approve stage 'board' after a full "
    "check without hard failures); the metrics are re-run at export time with the current "
    "board.md and must have no hard failure. Refused: not_approved (details.reason missing | "
    "stale | check_failed | changed_during_export), output_path_unsafe (fab/ or the target "
    "is a symlink or outside the project), export_failed (kicad-cli failed; the previous "
    "package stays unchanged). Result: dir, files [{path, bytes}], board_hash, swept "
    "(leftovers of crashed exports removed), notes (BOM/CPL are not validated for JLC "
    "assembly), next. The KiCad files are never changed; cost 0. Afterwards offer the user "
    "a git commit of fab/."
)

PCB_EXPORT_3D_SCHEMA = {
    "type": "object",
    "properties": {
        "format": {
            "type": "string",
            "enum": list(FORMATS),
            "description": "step (CAD exchange, case fit) or glb (glTF binary, viewers).",
        },
    },
    "required": ["format"],
    "additionalProperties": False,
}

PCB_EXPORT_3D_DESCRIPTION = (
    "Export the board as a 3D model to fab/3d/<board>.step or .glb (replaced atomically; "
    "no approval needed). Bare board: no 3D part models in this version. Refused: "
    "output_path_unsafe, export_failed (the old file stays). Result: path, bytes, "
    "board_hash, models, note. The KiCad files are never changed; cost 0."
)

FAB_SPECS: list[ToolSpec] = [
    ToolSpec(
        "pcb_fab_export",
        PCB_FAB_EXPORT_DESCRIPTION,
        PCB_FAB_EXPORT_SCHEMA,
        pcb_fab_export,
        mutating=True,
        changes_documents=False,
    ),
    ToolSpec(
        "pcb_export_3d",
        PCB_EXPORT_3D_DESCRIPTION,
        PCB_EXPORT_3D_SCHEMA,
        pcb_export_3d,
        mutating=True,
        changes_documents=False,
    ),
]
