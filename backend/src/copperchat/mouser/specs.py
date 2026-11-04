"""Tool provider ``mouser_search`` / ``mouser_part``: Mouser price, stock and lifecycle
lookups (mouser-api spec §4). Read-only; no KiCad session needed."""

from __future__ import annotations

from typing import Any

from copperchat.mouser.client import MAX_KEYWORD_RESULTS, MAX_MPNS, client_for, not_configured
from copperchat.tools import ToolContext, ToolResult, ToolSpec


async def _mouser_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    client = client_for(ctx.settings)
    if client is None:
        raise not_configured()
    return ToolResult(data=await client.search(args.get("keyword"), args.get("limit", 10)))


async def _mouser_part(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    client = client_for(ctx.settings)
    if client is None:
        raise not_configured()
    return ToolResult(data=await client.parts(args.get("mpns")))


SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "keyword": {
            "type": "string",
            "description": "Keyword or partial part number, e.g. 'buck converter 3A' or 'TPS6213'",
            "minLength": 1,
            "maxLength": 200,
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_KEYWORD_RESULTS,
            "description": "Number of hits (default 10)",
        },
    },
    "required": ["keyword"],
    "additionalProperties": False,
}
PART_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mpns": {
            "type": "array",
            "items": {"type": "string", "minLength": 3, "maxLength": 40},
            "minItems": 1,
            "maxItems": MAX_MPNS,
            "description": "Exact manufacturer (or Mouser) part numbers, e.g. ['TPS62133RGTR']",
        }
    },
    "required": ["mpns"],
    "additionalProperties": False,
}
SEARCH_DESCRIPTION = (
    "Search Mouser's catalogue by keyword: a short hit list (MPN, manufacturer, description, "
    "stock, qty-1 price, lifecycle). Use it to find the exact orderable part number, then "
    "mouser_part for details. Read-only; figures are Mouser's at today's date."
)
PART_DESCRIPTION = (
    "Mouser data for up to 10 exact part numbers in one call: price breaks (in the currency "
    "of the Mouser account), stock, on-order quantities, lead time, lifecycle (Active / "
    "New Product / Not Recommended for New Designs / End of Life / Obsolete / "
    "Discontinued), suggested replacement, datasheet URL, Mouser part number, minimum order "
    "and multiple, product URL. Parts Mouser does not list come back as not found. "
    "Read-only; figures are Mouser's at the returned as_of date."
)

TOOL_SPECS = [
    ToolSpec(
        name="mouser_search",
        description=SEARCH_DESCRIPTION,
        input_schema=SEARCH_SCHEMA,
        handler=_mouser_search,
        requires_kicad=False,
    ),
    ToolSpec(
        name="mouser_part",
        description=PART_DESCRIPTION,
        input_schema=PART_SCHEMA,
        handler=_mouser_part,
        requires_kicad=False,
    ),
]
