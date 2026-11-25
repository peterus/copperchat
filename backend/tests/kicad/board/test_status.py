"""`pcb_status` on the real api-server (SP4 P1c Task 4): boardless, the ecc83 demo board
and the placement hash after a footprint move through a board commit. The ecc83 tests
share one api-server (module scope); the moving test runs last."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from kipy.geometry import Vector2

from copperchat.board.model import BoardModel
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools.board import api
from copperchat.kicad_tools.board.read import load_board_model
from copperchat.kicad_tools.board.status import pcb_status
from tests.support.edit import edit_ctx

pytestmark = pytest.mark.kicad
MM = 1_000_000
HEX64 = re.compile(r"^[0-9a-f]{64}$")


async def test_status_boardless(blank_nopcb_session: KicadSession) -> None:
    ctx = edit_ctx(blank_nopcb_session)
    result = await pcb_status(ctx, {})
    assert result.data == {"exists": False, "suggestion": "run pcb_sync_from_schematic"}
    assert not result.changed_docs


@pytest.mark.asyncio(loop_scope="module")
async def test_status_ecc83(ecc83_module_session: KicadSession) -> None:
    s = ecc83_module_session
    ctx = edit_ctx(s)
    data = (await pcb_status(ctx, {})).data
    assert data is not None
    model: BoardModel = await s.call(lambda h: load_board_model(h, s.project), timeout=60)
    drc = await s.call(lambda h: api.run_drc_task(h), timeout=api.drc_call_timeout(60))
    assert data["exists"] is True
    assert data["footprints"] == len(model.parts)
    assert data["placed"] + data["unplaced"] == data["footprints"]
    assert len(data["unplaced_refs"]) + data["unplaced_omitted"] == data["unplaced"]
    assert data["copper_layers"] == len(model.copper_layers) == 2
    assert data["outline"] is not None and data["outline"]["w"] > 0
    assert data["unrouted"] == drc.unconnected
    assert data["routed"] == max(0, data["connections"] - data["unrouted"])
    assert data["connections"] > 0
    assert data["zones"] == 1
    assert HEX64.fullmatch(data["placement_hash"])
    assert HEX64.fullmatch(data["board_hash"])
    # a read tool: it never waits for the edit lock another tool call holds
    async with s.applying():
        again = await asyncio.wait_for(pcb_status(ctx, {}), timeout=120)
    assert again.data == data


@pytest.mark.asyncio(loop_scope="module")
async def test_status_hash_changes_after_move(ecc83_module_session: KicadSession) -> None:
    s = ecc83_module_session  # runs after test_status_ecc83: the move is the last change
    ctx = edit_ctx(s)
    before = (await pcb_status(ctx, {})).data
    assert before is not None

    def move(h: KicadHandle) -> Any:
        board = h.board()
        fp = sorted(board.get_footprints(), key=lambda f: f.reference_field.text.value)[0]
        fp.position = Vector2.from_xy(fp.position.x + 2 * MM, fp.position.y)
        board.update_items([fp])

    async with s.commit("copperchat: test move", doc="pcb"):
        await s.call(move)
    after = (await pcb_status(ctx, {})).data
    assert after is not None
    assert after["placement_hash"] != before["placement_hash"]
    assert after["board_hash"] != before["board_hash"]
