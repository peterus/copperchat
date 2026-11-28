"""The final board prompt and the default-on board tools (SP4 P9b Task 3)."""

from __future__ import annotations

import pytest

from copperchat.agent.profiles import PCB_WRITES, profile_specs
from copperchat.agent.sdk_backend import SYSTEM_PROMPT, system_prompt
from copperchat.config import Settings
from copperchat.tools import registry

# P1c's flag-off board paragraph, verbatim: COPPERCHAT_BOARD_TOOLS=off must not change a byte.
P1C_BOARD_OFF = (
    "- This is a board session. The schematic and the board are read-only here; use "
    "render_view and run_erc / run_drc to look at the design. Read the design documents with "
    "doc_list and doc_read, and record decisions with doc_append on decisions.md."
)


def test_prompt_mentions_both_skills() -> None:
    board = system_prompt("demo", "board", board_tools=True)
    assert "session-board" in board and "board-layout" in board
    assert "S0-S9" in board
    assert "design questions" in board


def test_prompt_states_checkpoints() -> None:
    board = system_prompt("demo", "board", board_tools=True)
    assert "the placement before any routing" in board
    assert "the final board before the fab export" in board
    assert "pcb_approve only in the turn started by the user's approving message" in board
    assert "The schematic is read-only here" in board


def test_flag_off_text_unchanged() -> None:
    expected = SYSTEM_PROMPT.format(project_name="demo", session_paragraph=P1C_BOARD_OFF)
    assert system_prompt("demo", "board", board_tools=False) == expected


def test_default_profiles_have_board_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COPPERCHAT_BOARD_TOOLS", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.board_tools is True
    registered = {s.name for s in registry.all_specs()} & PCB_WRITES
    assert registered == PCB_WRITES
    for kind in ("board", "free"):
        names = {
            s.name
            for s in profile_specs(
                kind,
                registry.all_specs(),
                board_tools=settings.board_tools,
                freerouting=True,
            )
        }
        assert names >= PCB_WRITES, (kind, sorted(PCB_WRITES - names))
    off = {s.name for s in profile_specs("board", registry.all_specs(), board_tools=False)}
    assert not PCB_WRITES & off
