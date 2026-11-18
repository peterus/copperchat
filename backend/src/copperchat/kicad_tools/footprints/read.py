"""Read-only footprint tools (spec §7.2/§7.3): `fp_get`, `fp_list_local`."""

from __future__ import annotations

from typing import Any, Literal, cast

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools import access
from copperchat.kicad_tools.filtering import natural_key
from copperchat.kicad_tools.footprints.model import SOURCE_FIELD, review_state
from copperchat.library.service import closest_footprints, library_for
from copperchat.tools.spec import ToolContext, ToolResult

MAX_LIB_ID_CHARS = 256
DETAILS = ("summary", "full")
MAX_QUERY_CHARS = 200
MAX_LIST_LIMIT = 200
DEFAULT_LIST_LIMIT = 50


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


def _query(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, str) or len(value) > MAX_QUERY_CHARS:
        raise _invalid(
            "Invalid query",
            f"query must be a string of at most {MAX_QUERY_CHARS} characters",
            "e.g. 'SRN3012' or 'inductor', or omit it",
        )
    return value.casefold().split()


def _list_limit(value: Any) -> int:
    if value is None:
        return DEFAULT_LIST_LIMIT
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid("Invalid limit", "limit must be an integer", f"1..{MAX_LIST_LIMIT}")
    if not 1 <= value <= MAX_LIST_LIMIT:
        raise _invalid("Invalid limit", f"limit must be 1..{MAX_LIST_LIMIT}", "e.g. 50")
    return value


def _footprint_users(nickname: str) -> Any:
    """lib_id -> sorted references of the open project's symbols whose Footprint field
    names a footprint of `nickname` (the symbol reader of sch_list_symbols)."""

    def run(h: KicadHandle) -> dict[str, list[str]]:
        users: dict[str, set[str]] = {}
        for sym in h.schematic().get_symbols():
            d = access.symbol_dict(sym)
            footprint = d["footprint"].strip()
            if footprint.partition(":")[0] == nickname:
                users.setdefault(footprint, set()).add(d["ref"])
        return {k: sorted(v, key=natural_key) for k, v in users.items()}

    return run


def _no_library(notes: list[dict[str, Any]], kind: str, message: str) -> ToolResult:
    notes.append({"kind": kind, "message": message})
    return ToolResult(data={"library": None, "footprints": [], "truncated": False, "notes": notes})


async def fp_list_local(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """The footprints of the repo's local library (spec §7.3). Without a project row
    for it the result is `library: null` with a note, and nothing is activated."""
    terms = _query(args.get("query"))
    limit = _list_limit(args.get("limit"))
    require_enabled(ctx)
    svc = library_for(ctx.session)
    notes: list[dict[str, Any]] = []
    info = await svc.local_library()
    if info is None:
        return _no_library(
            notes,
            "repo_root_unknown",
            "The repository root is unknown right now (git did not answer), so the "
            "local footprint library cannot be located. Try again later.",
        )
    lib = info.lib
    if info.row_conflict:
        return _no_library(notes, "library_conflict", info.row_conflict)
    if not info.row_present:
        on_disk = len(svc.local_names_on_disk(lib))
        message = (
            f"This project has no local footprint library yet ({lib.nickname}); the "
            "first fp_create adds it."
        )
        if on_disk:
            message += (
                f" The repo's library directory already holds {on_disk} footprint(s) "
                "from another project; they become usable here once a footprint tool "
                "adds the row."
            )
        return _no_library(notes, "no_local_library", message)
    geometries = await svc.local_footprints(lib, notes)
    users = await ctx.session.call(_footprint_users(lib.nickname))
    rows: list[dict[str, Any]] = []
    for g in sorted(geometries, key=lambda g: natural_key(g.lib_id)):
        name = g.lib_id.partition(":")[2]
        haystack = f"{name} {g.description}".casefold()
        if not all(t in haystack for t in terms):
            continue
        rows.append(
            {
                "name": name,
                "lib_id": g.lib_id,
                "description": g.description,
                "pad_count": len(g.pads),
                "needs_user_review": review_state(g, pending=False),
                "source": g.fields.get(SOURCE_FIELD),
                "used_by": users.get(g.lib_id, []),
            }
        )
    return ToolResult(
        data={
            "library": {
                "nickname": lib.nickname,
                "uri": lib.uri,
                "path_rel_repo": lib.dir.relative_to(lib.repo_root).as_posix(),
            },
            "footprints": rows[:limit],
            "truncated": len(rows) > limit,
            "notes": notes,
        }
    )
