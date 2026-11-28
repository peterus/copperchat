"""The system prompt with and without COPPERCHAT_BOARD_TOOLS (SP4 P1c Task 3)."""

from __future__ import annotations

import pytest

from copperchat.agent.sdk_backend import SYSTEM_PROMPT, system_prompt

# The pre-P1c session paragraphs (footprint tools on), kept verbatim: flag off must not
# change a byte.
_EDITING_OFF = (
    "- You can edit the schematic with the sch_* tools; load the kicad-schematic-editing "
    "skill before your first edit. The board (PCB) is read-only: if asked to change it, "
    "explain what you would change and that board editing is not available yet.\n"
    "- Footprints are read and checked with the fp_* tools and can be created in the "
    "repository's local footprint library; load the kicad-footprints skill before you "
    "create, approve or delete one.\n"
    "- Use render_view to look at the design when spatial reasoning matters, and run_erc / "
    "run_drc to check it.\n"
)
PRE_P1C = {
    "schematic": _EDITING_OFF + "- Read the design documents first (doc_list, then doc_read). "
    "Record design decisions with doc_append on decisions.md.",
    "board": "- This is a board session. The schematic and the board are read-only here; use "
    "render_view and run_erc / run_drc to look at the design. Read the design documents with "
    "doc_list and doc_read, and record decisions with doc_append on decisions.md.",
    "free": _EDITING_OFF + "- The design documents under docs/design/ (requirements, "
    "architecture, parts, decisions) are read and changed with the doc_* tools; load the "
    "design-docs skill before changing one.",
    "requirements": "- This is a requirements session. You work on the design documents under "
    "docs/design/ with the doc_* tools; you cannot change the schematic or board in this "
    "session. Load the session-requirements and design-docs skills first.\n"
    "- You may look at an existing design with render_view, run_erc / run_drc and the read "
    "tools for context.",
}


@pytest.mark.parametrize("kind", ["requirements", "schematic", "board", "free"])
def test_flag_off_unchanged(kind: str) -> None:
    expected = SYSTEM_PROMPT.format(project_name="demo", session_paragraph=PRE_P1C[kind])
    assert system_prompt("demo", kind) == expected
    assert system_prompt("demo", kind, board_tools=False) == expected


def test_flag_on_mentions_skill() -> None:
    board = system_prompt("demo", "board", board_tools=True)
    assert "session-board" in board
    assert (
        "The schematic is read-only here; you lay out the board step by step with the pcb_* "
        "tools." in board
    )
    assert "stop after each step and wait for the user's ok" in board
    assert "doc_patch_section" in board
    for kind in ("schematic", "free"):
        prompt = system_prompt("demo", kind, board_tools=True)
        assert "read-only: if asked to change it" not in prompt, kind
        assert (
            "The board (PCB) is laid out step by step with the pcb_* tools in a Board session "
            "(session-board skill)" in prompt
        ), kind
        assert "kicad-footprints" in prompt, kind
    assert "board-layout" in system_prompt("demo", "free", board_tools=True)
    assert "board-layout" not in system_prompt("demo", "schematic", board_tools=True)
    assert "session-board" not in system_prompt("demo", "requirements", board_tools=True)
