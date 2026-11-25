"""Live board view on the real api-server (SP4 P1c Task 7, G8): a board commit reported
through the history hook goes out, after the debounce, as one live frame whose board
view file (KiCad's own `SaveCopyOfDocument`) differs from the previous one. The track is
created through the API; no KiCad file is written by hand."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from kipy.board_types import Track
from kipy.geometry import Vector2
from kipy.proto.board.board_types_pb2 import BoardLayer

from copperchat.agent.engine import TurnInfo
from copperchat.agent.protocol import ViewUpdatedFrame
from copperchat.db import Database
from copperchat.history.service import HistoryService
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools.board.engine import (
    BoardChangeSet,
    BoardStage,
    BoardStageContext,
    apply_board_change_set,
)
from copperchat.kicad_tools.edit.changeset import Logical
from copperchat.views.snapshot import snapshot_path
from tests.kicad.board.conftest import _OneSession, board_ctx
from tests.support.conversations import TEST_CID
from tests.support.history import FakeEngine, FakeRuntime, RecordingBroadcaster

pytestmark = pytest.mark.kicad
MM = 1_000_000


def _add_track(h: KicadHandle) -> None:
    pad = next(p for p in h.board().get_pads() if p.net.name)
    t = Track()
    t.layer = BoardLayer.BL_F_Cu
    t.net = pad.net
    t.start = Vector2.from_xy(pad.position.x, pad.position.y)
    t.end = Vector2.from_xy(pad.position.x + 3 * MM, pad.position.y)
    t.width = 250_000
    h.board().create_items([t])


def _frames(broadcaster: RecordingBroadcaster) -> list[ViewUpdatedFrame]:
    return [f for f in broadcaster.frames if isinstance(f, ViewUpdatedFrame)]


async def test_board_commit_sends_one_live_board_frame(
    ecc83_session: KicadSession, board_db: Database
) -> None:
    session, project = ecc83_session, ecc83_session.project
    broadcaster = RecordingBroadcaster()
    engine = FakeEngine()
    svc = HistoryService(
        db=board_db,
        sessions=cast(Any, _OneSession(session)),
        broadcaster=cast(Any, broadcaster),
        runtime=cast(Any, FakeRuntime(engine)),
    )
    hooks = svc.hooks_factory(project, cast(Any, engine))
    await svc.publish_view(project, session, {"pcb"})  # what the clients have
    view = snapshot_path(project, "pcb")
    before = view.read_bytes()

    async def stage(sctx: BoardStageContext) -> None:
        await sctx.call(_add_track)

    cs = BoardChangeSet(
        tool="pcb_test",
        logical=Logical(1),
        kind="board_move",
        board_stages=[BoardStage("track", stage)],
    )
    result = await apply_board_change_set(board_ctx(session, board_db), cs)
    assert result.changed_docs == {"pcb"}
    turn = TurnInfo(run_id="run-1", message_id=1, text="t", metadata={}, conversation_id=TEST_CID)
    hooks.on_documents_changed(turn, {"pcb"})  # as the adapter does after apply
    try:
        async with asyncio.timeout(60):
            while len(_frames(broadcaster)) < 2:
                await asyncio.sleep(0.1)
        await asyncio.sleep(svc.live.debounce_s + svc.live.min_interval_s)  # no second frame
    finally:
        sent = await svc.live.finish(project)
    frames = _frames(broadcaster)
    assert len(frames) == 2 and sent == {"pcb"}
    live = frames[1]
    assert live.live is True and live.docs == ["pcb"]
    assert live.changed_files == [view.name]
    assert view.read_bytes() != before
