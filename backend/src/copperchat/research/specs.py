"""Tool provider ``research_part`` (index "Decided by owner" item 8)."""

from __future__ import annotations

from typing import Any

from copperchat.tools import StructuredToolError, ToolContext, ToolResult, ToolSpec


async def _research_part(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.research is None:
        raise StructuredToolError(
            "Part research is not available",
            "this CopperChat agent backend has no part-researcher",
            "Answer without research and say that the datasheet was not checked",
        )
    return ToolResult(text=await ctx.research.run(args))


TOOL_SPECS = [
    ToolSpec(
        name="research_part",
        description=(
            "Ask the part-researcher about one electronic part: pinout, unused pins, ratings, "
            "application circuit with component values and formulas, required external parts, "
            "layout rules, package and KiCad footprint. It reads or creates "
            "docs/parts/<MPN>/notes.md (from the web / the datasheet) and returns a summary. "
            "Takes minutes; ask one complete question per part."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "mpn": {
                    "type": "string",
                    "description": "Bare manufacturer part number, e.g. TPS62133",
                    "minLength": 1,
                    "maxLength": 64,
                },
                "query": {
                    "type": "string",
                    "description": "The question, with the operating point if relevant",
                    "minLength": 1,
                },
                "attachment_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Paths of attached PDFs/images as given in the message",
                },
            },
            "required": ["mpn", "query"],
            "additionalProperties": False,
        },
        handler=_research_part,
    )
]
