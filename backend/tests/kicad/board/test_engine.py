"""The board write engine on the real api-server (SP4 P1a Task 7, B-C4).

A three-stage test change set built here: stage 1 adds a track, stage 2 adds a via and
moves a footprint 2 mm, stage 3 adds a zone and a group around an existing footprint.
A failure after any stage must leave the board as it was (hashes and KIID sets).
Every item is created through the API; no KiCad file is written by hand.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from kipy.board_types import Group, Track, Via, Zone
from kipy.geometry import Angle, PolygonWithHoles, PolyLine, PolyLineNode, Vector2
from kipy.proto.board.board_types_pb2 import BoardLayer

from copperchat.board.model import board_hash, placement_hash
from copperchat.db import Database
from copperchat.errors import StructuredToolError
from copperchat.history.service import HistoryService
from copperchat.kicad_session import KicadHandle, KicadSession, KicadSessionManager
from copperchat.kicad_tools.board import api
from copperchat.kicad_tools.board.api import set_group_name
from copperchat.kicad_tools.board.compensate import take_snapshot
from copperchat.kicad_tools.board.engine import (
    BoardChangeSet,
    BoardStage,
    BoardStageContext,
    apply_board_change_set,
)
from copperchat.kicad_tools.board.read import load_board_model
from copperchat.kicad_tools.edit.changeset import Logical
from tests.kicad.board.conftest import board_ctx

pytestmark = pytest.mark.kicad
MM = 1_000_000


async def _state(session: KicadSession) -> tuple[str, str, dict[str, frozenset[str]]]:
    project = session.project

    def read(h: KicadHandle) -> tuple[str, str, dict[str, frozenset[str]]]:
        m = load_board_model(h, project)
        return placement_hash(m), board_hash(m), take_snapshot(h, "ids").ids()

    return await session.call(read)


def _fps(h: KicadHandle) -> list[Any]:
    return sorted(h.board().get_footprints(), key=lambda f: f.reference_field.text.value)


def _net_pad(h: KicadHandle) -> Any:
    return next(p for p in h.board().get_pads() if p.net.name)


def _add_track(h: KicadHandle) -> str:
    pad = _net_pad(h)
    t = Track()
    t.layer = BoardLayer.BL_F_Cu
    t.net = pad.net
    t.start = Vector2.from_xy(pad.position.x, pad.position.y)
    t.end = Vector2.from_xy(pad.position.x + 3 * MM, pad.position.y)
    t.width = 250_000
    (created,) = h.board().create_items([t])
    return str(created.id.value)


def _add_via_and_move(h: KicadHandle) -> str:
    board = h.board()
    pad = _net_pad(h)
    v = Via()
    v.position = Vector2.from_xy(pad.position.x + 3 * MM, pad.position.y + 3 * MM)
    v.net = pad.net
    layer = v.padstack.proto.copper_layers.add()
    layer.layer = BoardLayer.BL_F_Cu
    v.diameter = 600_000
    v.drill_diameter = 300_000
    (created,) = board.create_items([v])
    fp = _fps(h)[0]
    fp.position = Vector2.from_xy(fp.position.x + 2 * MM, fp.position.y)
    board.update_items([fp])
    return str(created.id.value)


def _add_zone_and_group(h: KicadHandle) -> list[str]:
    board = h.board()
    pad = _net_pad(h)
    x, y = pad.position.x, pad.position.y
    z = Zone()
    z.layers = [BoardLayer.BL_B_Cu]
    z.net = pad.net
    line = PolyLine()
    for dx, dy in ((0, 0), (5, 0), (5, 5), (0, 5)):
        line.append(PolyLineNode.from_xy(x + dx * MM, y + dy * MM))
    line.closed = True
    poly = PolygonWithHoles()
    poly.outline = line
    z.outline = poly
    g = Group()
    g.item_ids = [_fps(h)[1].id]
    set_group_name(g.proto, "blk:test")
    created = board.create_items([z, g])
    return [str(c.id.value) for c in created]


def _stages(gate: asyncio.Event | None = None, entered: asyncio.Event | None = None) -> list[Any]:
    async def s1(sctx: BoardStageContext) -> None:
        sctx.created.add(await sctx.call(_add_track))

    async def s2(sctx: BoardStageContext) -> None:
        sctx.created.add(await sctx.call(_add_via_and_move))
        if gate is not None:
            assert entered is not None
            entered.set()
            await gate.wait()

    async def s3(sctx: BoardStageContext) -> None:
        sctx.created.update(await sctx.call(_add_zone_and_group))
        sctx.report["created"] = len(sctx.created)

    return [BoardStage("track", s1), BoardStage("via+move", s2), BoardStage("zone", s3)]


def _cs(stages: list[Any], **kw: Any) -> BoardChangeSet:
    return BoardChangeSet(
        tool="pcb_test", logical=Logical(1), kind="board_move", board_stages=stages, **kw
    )


@pytest.mark.parametrize("fail_after", [1, 2, 3])
async def test_fail_after_restores_hashes(
    ecc83_session: KicadSession, board_db: Database, fail_after: int
) -> None:
    s = ecc83_session
    before = await _state(s)
    with pytest.raises(RuntimeError, match="fail_after"):
        await apply_board_change_set(board_ctx(s, board_db), _cs(_stages(), fail_after=fail_after))
    assert await _state(s) == before


async def test_cancel_during_stage_2(ecc83_session: KicadSession, board_db: Database) -> None:
    s = ecc83_session
    before = await _state(s)
    gate, entered = asyncio.Event(), asyncio.Event()
    task = asyncio.ensure_future(
        apply_board_change_set(board_ctx(s, board_db), _cs(_stages(gate, entered)))
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert s.server_alive
    assert await _state(s) == before


async def test_full_snapshot_recreates_deleted_track(
    ecc83_session: KicadSession, board_db: Database
) -> None:
    s = ecc83_session
    before = await _state(s)

    def delete_one(h: KicadHandle) -> str:
        board = h.board()
        t = sorted(board.get_tracks(), key=lambda t: str(t.id.value))[0]
        board.remove_items([t])
        return str(t.id.value)

    deleted: list[str] = []

    async def s1(sctx: BoardStageContext) -> None:
        deleted.append(await sctx.call(delete_one))

    async def s2(sctx: BoardStageContext) -> None:
        raise ValueError("stage 2 failed")

    cs = _cs([BoardStage("delete", s1), BoardStage("fail", s2)], snapshot="full")
    with pytest.raises(ValueError, match="stage 2 failed"):
        await apply_board_change_set(board_ctx(s, board_db), cs)
    after = await _state(s)
    assert after[:2] == before[:2]
    print("re-created track keeps its KIID:", deleted[0] in after[2]["track"])
    assert len(after[2]["track"]) == len(before[2]["track"])


async def test_flip_and_track_change_restored(
    ecc83_session: KicadSession, board_db: Database
) -> None:
    """A footprint flipped to the other side and rotated, and an existing track made
    wider ("full"), are put back by the compensation."""
    s = ecc83_session
    before = await _state(s)

    def change(h: KicadHandle) -> None:
        board = h.board()
        fp = _fps(h)[2]
        (flipped,) = board.flip_items([fp])
        flipped.orientation = Angle.from_degrees(90)
        t = sorted(board.get_tracks(), key=lambda t: str(t.id.value))[0]
        t.width = t.width + 100_000
        board.update_items([flipped, t])

    async def s1(sctx: BoardStageContext) -> None:
        await sctx.call(change)

    async def s2(sctx: BoardStageContext) -> None:  # the change really landed
        changed = await _state(s)
        sctx.report["changed"] = changed[0] != before[0] and changed[1] != before[1]

    cs = _cs([BoardStage("flip", s1), BoardStage("check", s2, commit=False)], snapshot="full",
             fail_after=2)  # fmt: skip
    with pytest.raises(RuntimeError, match="fail_after"):
        await apply_board_change_set(board_ctx(s, board_db), cs)
    assert cs.report["changed"] is True
    assert await _state(s) == before


async def test_success_changed_docs(ecc83_session: KicadSession, board_db: Database) -> None:
    s = ecc83_session
    before = await _state(s)
    ctx = board_ctx(s, board_db)
    result = await apply_board_change_set(ctx, _cs(_stages()))
    assert result.changed_docs == {"pcb"}
    assert s.modified_docs == {"pcb"}
    assert result.data is not None
    assert result.data["created"] == 4
    assert [r["stage"] for r in result.data["step_report"]] == ["track", "via+move", "zone"]
    after = await _state(s)
    assert after[0] != before[0] and after[1] != before[1]
    assert ctx.editing is not None
    rows = await ctx.editing.changes.for_run(s.project.id, ctx.run_id or "")
    assert len(rows) == 1
    assert rows[0].kind == "board_move" and rows[0].sheet_path == ""
    assert rows[0].after == {
        "tracks_added": 1,
        "vias_added": 1,
        "zones_added": 1,
        "groups_added": 1,
        "footprints_moved": 1,
    }


# -- board creation ---------------------------------------------------------------------


def _empty_stage() -> BoardStage:
    async def run(sctx: BoardStageContext) -> None:
        return None

    return BoardStage("empty", run)


async def test_creates_board_then_reopens(
    blank_nopcb_sessions: KicadSessionManager, board_db: Database
) -> None:
    old = blank_nopcb_sessions.current()
    assert old is not None
    ctx = board_ctx(old, board_db, blank_nopcb_sessions)
    assert ctx.on_mutation is not None
    await ctx.on_mutation()  # the turn snapshot records the board absent
    result = await apply_board_change_set(ctx, _cs([_empty_stage()], creates_board=True))
    assert result.changed_docs == {"pcb"}
    pcb = ctx.project.pro_file.with_suffix(".kicad_pcb")
    assert pcb.is_file()
    assert ctx.project.pcb_file == pcb
    assert ctx.session is not old and ctx.session is blank_nopcb_sessions.current()
    assert old.closed and not ctx.session.closed
    assert ctx.reloaded_generation == ctx.session.generation
    assert HistoryService.docs_of(ctx.project) == {"sch", "pcb"}
    # "pcb" is open in the new api-server: a board read works
    assert await ctx.session.call(lambda h: len(h.board().get_footprints())) == 0


async def test_creates_board_failure_removes_it(
    blank_nopcb_sessions: KicadSessionManager, board_db: Database
) -> None:
    old = blank_nopcb_sessions.current()
    assert old is not None
    ctx = board_ctx(old, board_db, blank_nopcb_sessions)
    pro = ctx.project.pro_file
    pro_before = pro.read_bytes()
    seen: dict[str, bytes] = {}

    async def look(sctx: BoardStageContext) -> None:
        seen["pro"] = pro.read_bytes()  # after KiCad created the board and reopened

    cs = _cs([BoardStage("look", look)], creates_board=True, fail_after=1)
    with pytest.raises(RuntimeError, match="fail_after"):
        await apply_board_change_set(ctx, cs)
    assert not pro.with_suffix(".kicad_pcb").exists()
    assert ctx.project.pcb_file is None
    assert HistoryService.docs_of(ctx.project) == {"sch"}
    assert ctx.session is blank_nopcb_sessions.current() and not ctx.session.closed
    rewritten = seen["pro"] != pro_before
    print(".kicad_pro rewritten by KiCad during board creation:", rewritten)
    print(".kicad_pro differs after the failed call:", pro.read_bytes() != pro_before)


@pytest.mark.parametrize("target_exists", [False, True])
async def test_creates_board_symlinked_pcb_refused(
    blank_nopcb_sessions: KicadSessionManager,
    board_db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_exists: bool,
) -> None:
    session = blank_nopcb_sessions.current()
    assert session is not None
    ctx = board_ctx(session, board_db, blank_nopcb_sessions)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "x.kicad_pcb"
    if target_exists:
        target.write_bytes(b"not a board, never touched\n")
    link = ctx.project.pro_file.with_suffix(".kicad_pcb")
    os.symlink(target, link)
    listing = sorted(os.listdir(outside))
    content = target.read_bytes() if target_exists else None
    calls: list[Any] = []
    monkeypatch.setattr(api, "create_board_document", lambda *a: calls.append(a))
    with pytest.raises(StructuredToolError) as info:
        await apply_board_change_set(ctx, _cs([_empty_stage()], creates_board=True))
    assert info.value.code == "output_path_unsafe"
    assert info.value.details == {"path": link.name}
    assert calls == []
    assert sorted(os.listdir(outside)) == listing
    assert (target.read_bytes() if target_exists else None) == content
    assert link.is_symlink() and ctx.session is session


async def test_creates_board_dir_in_the_way_refused(
    blank_nopcb_sessions: KicadSessionManager, board_db: Database
) -> None:
    session = blank_nopcb_sessions.current()
    assert session is not None
    ctx = board_ctx(session, board_db, blank_nopcb_sessions)
    d = ctx.project.pro_file.with_suffix(".kicad_pcb")
    d.mkdir()
    with pytest.raises(StructuredToolError) as info:
        await apply_board_change_set(ctx, _cs([_empty_stage()], creates_board=True))
    assert info.value.code == "output_path_unsafe"
    assert d.is_dir() and ctx.session is session
