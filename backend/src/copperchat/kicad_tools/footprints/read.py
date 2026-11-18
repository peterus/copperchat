"""Read-only footprint tools (spec §7.2/§7.3): `fp_get`."""

from __future__ import annotations

from typing import Any, Literal, cast

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.footprints.model import SOURCE_FIELD, review_state
from copperchat.library.service import closest_footprints, library_for
from copperchat.tools.spec import ToolContext, ToolResult

MAX_LIB_ID_CHARS = 256
DETAILS = ("summary", "full")


def _invalid(what: str, why: str, suggestion: str) -> StructuredToolError:
    return StructuredToolError(what, why, suggestion, code="invalid_argument")


def require_enabled(ctx: ToolContext) -> None:
    """Defence in depth: the profiles drop every `fp_*` tool when the switch is off."""
    if not ctx.settings.footprint_tools:
        raise StructuredToolError(
            "Footprint tools are disabled",
            "COPPERCHAT_FOOTPRINT_TOOLS is off on this server",
            "Pick footprints with lib_search_footprints instead.",
            code="footprint_tools_disabled",
        )


def parse_lib_id(value: Any) -> tuple[str, str, str]:
    """(lib_id, nickname, name) of a `<nickname>:<name>` argument, else invalid_argument."""
    if not isinstance(value, str) or len(value) > MAX_LIB_ID_CHARS:
        raise _invalid(
            "Invalid lib_id",
            f"lib_id must be a string of at most {MAX_LIB_ID_CHARS} characters",
            "e.g. 'Inductor_SMD:L_Bourns-SRN4018'",
        )
    lib_id = value.strip()
    nickname, sep, name = lib_id.partition(":")
    if not sep or not nickname or not name:
        raise _invalid(
            f"Invalid lib_id {lib_id[:80]!r}",
            "expected <library nickname>:<footprint name>",
            "e.g. 'Inductor_SMD:L_Bourns-SRN4018'; find ids with lib_search_footprints",
        )
    return lib_id, nickname, name


async def fp_get(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """One footprint's geometry (spec §7.2): `FootprintGeometry.to_dict(detail)` plus
    `source`, `review` (local footprints only, else null) and `notes`."""
    lib_id, nickname, name = parse_lib_id(args.get("lib_id"))
    detail = args.get("detail", "summary")
    if detail not in DETAILS:
        raise _invalid(
            "Invalid detail", "detail must be 'summary' or 'full'", "omit it for 'summary'"
        )
    require_enabled(ctx)
    svc = library_for(ctx.session)
    notes: list[dict[str, Any]] = []
    geometry = await svc.footprint_geometry(lib_id, notes)
    if geometry is None:
        close = await closest_footprints(svc, nickname, name)
        raise StructuredToolError(
            f"Footprint {lib_id!r} is not in the libraries",
            "unknown footprint (format <library nickname>:<footprint name>, case-sensitive)",
            f"Closest matches: {close}" if close else "Use lib_search_footprints.",
            code="not_found",
            details={"closest": close},
        )
    data = geometry.to_dict(cast(Literal["summary", "full"], detail))
    data["source"] = geometry.fields.get(SOURCE_FIELD)
    data["review"] = review_state(geometry, pending=False) if geometry.scope == "local" else None
    data["notes"] = notes
    return ToolResult(data=data)
