"""Session types as tool profiles (spec §8.2, plan S-C7).

A profile is the frozen set of CopperChat tool names a session type may use. It filters
the MCP server of a burst and is enforced again by ``can_use_tool`` and the PreToolUse
hook (both built from the filtered list). New tools default to Free only: every other
profile opts in explicitly, and a unit test keeps Free a superset of the registry.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from copperchat.tools import ToolContext, ToolSpec

if TYPE_CHECKING:
    from copperchat.db.repos.conversations import ConversationRow

READ_KICAD = frozenset(
    {
        "project_info",
        "sch_list_symbols",
        "sch_get_symbol",
        "sch_get_net",
        "sch_get_pin",
        "pcb_list_footprints",
        "pcb_get_footprint",
        "pcb_get_net",
        "run_erc",
        "run_drc",
        "render_view",
        "sch_find_space",
        "sch_changes",
    }
)
LIBRARY = frozenset({"lib_search_symbols", "lib_get_symbol", "lib_search_footprints"})
PARTS = frozenset({"parts_get_notes", "research_part"})
GIT = frozenset({"git_status", "git_diff_summary", "git_commit"})
SCH_WRITES = frozenset(
    {
        "sch_add_wire",
        "sch_connect",
        "sch_add_label",
        "sch_add_power",
        "sch_no_connect",
        "sch_rename_net",
        "sch_reroute",
        "sch_place_symbol",
        "sch_set_fields",
        "sch_annotate",
        "sch_add_sheet",
        "sch_add_sheet_pin",
        "sch_move",
        "sch_delete",
        "sch_duplicate",
    }
)
PLAN_TOOLS = frozenset({"plan_propose", "plan_revise", "plan_step_begin", "plan_step_done"})
DOC_TOOLS = frozenset({"doc_list", "doc_read", "doc_write", "doc_patch_section", "doc_append"})
# The 40 tools of e2d73b5 (spec §8.1).
TODAY = READ_KICAD | LIBRARY | PARTS | GIT | SCH_WRITES | PLAN_TOOLS

PROFILES: dict[str, frozenset[str]] = {
    "requirements": DOC_TOOLS
    | PARTS
    | LIBRARY
    | (READ_KICAD - {"sch_find_space", "sch_changes"})
    | GIT,
    "schematic": TODAY | {"doc_list", "doc_read", "doc_append", "doc_patch_section"},
    # SP4 (hidden): the read, ERC/DRC, git, library and parts tools; later the pcb_* writes.
    "board": READ_KICAD | LIBRARY | PARTS | GIT | {"doc_list", "doc_read", "doc_append"},
    "free": TODAY | DOC_TOOLS,
}


def profile_specs(conversation_type: str, specs: Sequence[ToolSpec]) -> list[ToolSpec]:
    """The specs a session of ``conversation_type`` may use, in registry order. Free gets
    every registered tool (also one added after this file was written); an unknown type
    raises (fail closed)."""
    if conversation_type == "free":
        return list(specs)
    allowed = PROFILES.get(conversation_type)
    if allowed is None:
        raise ValueError(f"unknown session type: {conversation_type!r}")
    return [s for s in specs if s.name in allowed]


def bind_conversation(ctx: ToolContext, conversation: ConversationRow | None) -> ToolContext:
    """``ctx`` with the burst's conversation fields (S-C5). Burst-scoped, so a turn the CLI
    starts on its own still gets its session's policy."""
    if conversation is None:
        return ctx
    return replace(
        ctx,
        conversation_id=conversation.id,
        conversation_type=conversation.type,
        conversation_seq=conversation.seq,
        conversation_title=conversation.title,
    )
