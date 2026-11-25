"""The board tool provider is registered and its read tools reach every profile (SP4 P1c
Task 4)."""

from __future__ import annotations

from typing import Any

import pytest

from copperchat.agent.profiles import PROFILES, profile_specs
from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.board.views import pcb_render
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


@pytest.mark.parametrize(
    "extra",
    [
        {"markers": True},
        {"highlight_nets": ["GND"]},
        {"region": {"x": 0, "y": 0, "w": 10, "h": 10}},
    ],
)
async def test_render_3d_refuses_2d_overlays(extra: dict[str, Any]) -> None:
    """The 3D view draws no overlays: refused before the board (or a DRC) is touched."""
    ctx: Any = object()  # any use of the context would raise AttributeError
    with pytest.raises(StructuredToolError) as err:
        await pcb_render(ctx, {"view": "3d", **extra})
    assert err.value.code == "invalid_argument"
