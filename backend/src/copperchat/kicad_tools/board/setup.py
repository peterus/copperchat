"""Board setup tools (spec §7.2 "Setup"): `pcb_set_outline` (rounded-rectangle outline,
mounting holes, keepout rule areas) and `pcb_set_rules` (layer count, fab preset rules,
net classes, custom rules). Every write goes through the board engine (B-C4): one commit
per stage, compensated on failure; non-item state (rules, classes, layer count) is
restored by each stage's `undo`."""

from __future__ import annotations

from copperchat.errors import StructuredToolError
from copperchat.tools.spec import ToolContext


def require_board_tools(ctx: ToolContext) -> None:
    """Refuse a board write while COPPERCHAT_BOARD_TOOLS is off (B-C14). The profiles
    already drop the tools then; this guards every other path."""
    if not ctx.settings.board_tools:
        raise StructuredToolError(
            "Board tools are disabled",
            "COPPERCHAT_BOARD_TOOLS is off on this server",
            "Tell the user; an admin can enable board tools.",
            code="board_tools_disabled",
        )


# -- pcb_set_outline -------------------------------------------------------------------


# -- pcb_set_rules ---------------------------------------------------------------------
