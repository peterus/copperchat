from __future__ import annotations

from typing import Any

from copperchat.tools.spec import ToolContext, ToolResult, ToolSpec
from copperchat.views.region import Region
from copperchat.views.render import render_view

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["doc", "mode"],
    "properties": {
        "doc": {"enum": ["sch", "pcb"]},
        "mode": {
            "enum": ["agent", "kicad"],
            "description": "agent: mm grid, labelled footprints with positions, net colours, "
            "airwires and DRC/ERC markers; kicad: KiCad's own plot.",
        },
        "region": {
            "type": "object",
            "additionalProperties": False,
            "description": "Zoom: a mm box (x_mm, y_mm, w_mm, h_mm) or around_ref.",
            "properties": {
                "x_mm": {"type": "number"},
                "y_mm": {"type": "number"},
                "w_mm": {"type": "number", "exclusiveMinimum": 0},
                "h_mm": {"type": "number", "exclusiveMinimum": 0},
                "around_ref": {"type": "string"},
                "margin_mm": {"type": "number", "minimum": 0},
            },
        },
        "layers": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Board only: KiCad layer names, e.g. ['F.Cu', 'Edge.Cuts'].",
        },
        "sheet": {
            "type": "string",
            "description": "Schematic only: sheet path ('/amp/') or name; default root sheet.",
        },
        "theme": {"enum": ["agent", "light", "dark"]},
    },
}


async def _handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    region = Region(**args["region"]) if args.get("region") else None
    img = await render_view(
        ctx.session,
        args["doc"],
        args["mode"],
        region=region,
        layers=args.get("layers"),
        theme=args.get("theme", "agent"),
        sheet=args.get("sheet"),
    )
    return ToolResult(data={"image": img.path.name if img.path else None}, images=[img])


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        "render_view",
        "Render the schematic or board as an image you can look at. Use region (mm box or "
        "around_ref) to zoom into dense areas and layers to isolate board layers.",
        _SCHEMA,
        _handler,
    ),
]
