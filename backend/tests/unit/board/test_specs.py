"""The board tool provider is registered and its read tools reach every profile (SP4 P1c
Task 4)."""

from __future__ import annotations

from copperchat.agent.profiles import PROFILES, profile_specs
from copperchat.tools.registry import _PROVIDERS, all_specs

BOARD_PROVIDER = "copperchat.kicad_tools.board.specs"


def test_status_registered() -> None:
    assert BOARD_PROVIDER in _PROVIDERS
    specs = all_specs()
    status = next(s for s in specs if s.name == "pcb_status")
    assert not status.mutating and not status.changes_documents
    assert status.prepare is None and status.apply is None
    for kind in PROFILES:
        names = {s.name for s in profile_specs(kind, specs, board_tools=False)}
        assert "pcb_status" in names, kind
