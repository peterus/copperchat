"""The board render's DRC markers degrade to none when the heavy-process gate is busy
(SP4 round 2 R6): a running fill or parity DRC never fails a render."""

from __future__ import annotations

from typing import Any

import pytest

from copperchat.errors import StructuredToolError
from copperchat.views import render


def _gate_timeout() -> StructuredToolError:
    from copperchat.board.heavy import heavy_timeout

    return heavy_timeout("freerouting", 120.0)


@pytest.mark.parametrize("code", ["board_compute_timeout", "other"])
async def test_render_markers_degrade_on_gate_timeout(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    drawn: list[Any] = []

    async def snapshot(session: Any) -> dict[str, Any]:
        return {}

    async def markers(session: Any, doc: str) -> list[dict[str, Any]]:
        if code == "board_compute_timeout":
            raise _gate_timeout()
        raise StructuredToolError("DRC failed", "boom", "retry", code=code)

    def draw(snap: Any, **kw: Any) -> bytes:
        drawn.append(kw["markers"])
        return b"png"

    monkeypatch.setattr(render, "snapshot_with_connectivity", snapshot)
    monkeypatch.setattr(render, "resolve_region", lambda region, snap: None)
    monkeypatch.setattr(render, "cached_violations", markers)
    monkeypatch.setattr(render, "render_board", draw)
    if code == "board_compute_timeout":
        assert await render._agent_board(object(), None, None, "agent") == b"png"  # type: ignore[arg-type]
        assert drawn == [[]]
    else:  # any other check failure still fails the render as before
        with pytest.raises(StructuredToolError):
            await render._agent_board(object(), None, None, "agent")  # type: ignore[arg-type]
