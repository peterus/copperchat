from __future__ import annotations

from typing import Any

from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools import access
from copperchat.tools.spec import ToolContext, ToolResult


def _board_info(h: KicadHandle) -> dict[str, Any]:
    board = h.board()
    shapes, _ = access.outline_segments(board)
    boxes = [b for b in access.bounding_boxes(board, shapes) if b is not None]
    outline: dict[str, Any] | None = None
    if boxes:
        x0 = min(b["x"] for b in boxes)
        y0 = min(b["y"] for b in boxes)
        x1 = max(b["x"] + b["w"] for b in boxes)
        y1 = max(b["y"] + b["h"] for b in boxes)
        outline = {
            "origin_mm": {"x": x0, "y": y0},
            "width_mm": round(x1 - x0, 4),
            "height_mm": round(y1 - y0, 4),
        }
    return {
        "board": outline,
        "layers": [access.layer_name(layer) for layer in board.get_enabled_layers()],
        "copperLayers": int(board.get_copper_layer_count()),
        "netCount": sum(1 for n in board.get_nets() if n.name),
        "footprintCount": len(board.get_footprints()),
        "designRules": access.design_rules(board),
    }


def _schematic_info(h: KicadHandle) -> dict[str, Any]:
    sch = h.schematic()
    refs = {str(s.reference) for s in sch.get_symbols()}
    return {
        "sheets": access.hierarchy(sch),
        # One entry per part: units and sheet instances share a reference; power
        # symbols (#PWR...) are not parts.
        "symbolCount": sum(1 for r in refs if not r.startswith("#")),
    }


async def project_info(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    proj = ctx.project
    data: dict[str, Any] = {
        "name": proj.name,
        "kicadVersion": await ctx.session.call(lambda h: str(h.kicad.get_version().full_version)),
        "readOnly": ctx.session.read_only,
        "files": {
            "project": proj.pro_file.name,
            "schematic": proj.sch_file.name if proj.sch_file else None,
            "board": proj.pcb_file.name if proj.pcb_file else None,
        },
        "units": "mm",
    }
    if proj.sch_file is not None:
        data |= await ctx.session.call(_schematic_info)
    if proj.pcb_file is not None:
        data |= await ctx.session.call(_board_info)
    return ToolResult(data=data)
