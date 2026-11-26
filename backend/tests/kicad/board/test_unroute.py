"""`pcb_unroute` (delete and `unlock_only`) on the real api-server (SP4 P5b Task 3), on
`synth_placed` with two routed signal nets — `UNLOCKED` routed `lock: false`, `LOCKED`
routed `lock: true` — and a locked GND stub + via made through `apply_board_change_set`.

One module server; each test first restores the state it needs (`_ensure`)."""

from __future__ import annotations

import dataclasses
import math
from typing import Any

import pytest
import pytest_asyncio
from kipy.board_types import Track as KipyTrack
from kipy.board_types import Via as KipyVia
from kipy.proto.board.board_types_pb2 import BoardLayer

from copperchat.board.model import BoardModel, Rect, board_hash
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.board import api, route
from copperchat.kicad_tools.board.engine import (
    BoardChangeSet,
    BoardStage,
    BoardStageContext,
    apply_board_change_set,
)
from copperchat.kicad_tools.board.read import BOARD_READ_TIMEOUT_S, board_model
from copperchat.kicad_tools.edit.changeset import Logical
from copperchat.kicad_tools.units import mm_to_nm
from tests.kicad.board.conftest import PlacedSynth, setup_call
from tests.support.edit import call_tool

pytestmark = [pytest.mark.kicad, pytest.mark.asyncio(loop_scope="module")]

UNLOCKED = "/CC1"
LOCKED = "/BOOT0"


async def _model(placed: PlacedSynth, run: str) -> BoardModel:
    return await board_model(placed.turn(run))


def _items(m: BoardModel, net: str) -> list[Any]:
    return [t for t in m.tracks if t.net == net] + [v for v in m.vias if v.net == net]


async def _ensure(placed: PlacedSynth, run: str, net: str, *, lock: bool) -> None:
    """`net` routed fresh with `lock`."""
    m = await _model(placed, f"{run}-m")
    if _items(m, net):
        await setup_call(placed.turn(f"{run}-u"), "pcb_unroute", nets=[net], include_locked=True)
    out = await setup_call(placed.turn(f"{run}-r"), "pcb_route", nets=[net], lock=lock)
    assert out["nets"][0]["status"] == "routed", out


def _stub_items(h: KicadHandle, x: float, y: float, dx: float, dy: float) -> list[str]:
    board = h.board()
    gnd = next(n for n in board.get_nets() if n.name == "GND")
    end = (x + dx, y + dy)
    t = KipyTrack()
    t.layer = BoardLayer.BL_F_Cu
    t.net = gnd
    t.start = route._vec((x, y))
    t.end = route._vec(end)
    t.width = mm_to_nm(0.3)
    t.locked = True
    v = KipyVia()
    v.position = route._vec(end)
    v.net = gnd
    v.padstack.proto.copper_layers.add().layer = BoardLayer.BL_F_Cu
    v.diameter = mm_to_nm(0.6)
    v.drill_diameter = mm_to_nm(0.3)
    v.locked = True
    return [str(c.id.value) for c in board.create_items([t, v])]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def rig(synth_placed: PlacedSynth) -> dict[str, Any]:
    """Both nets routed and a locked GND stub (1 mm track + via) from the GND pad nearest
    the locked net's copper, pointing away from its part."""
    placed = synth_placed
    await _ensure(placed, "run-rig-1", UNLOCKED, lock=False)
    await _ensure(placed, "run-rig-2", LOCKED, lock=True)
    m = await _model(placed, "run-rig-3")
    mine = [t for t in m.tracks if t.net == LOCKED]
    mid = ((mine[0].start[0] + mine[0].end[0]) / 2, (mine[0].start[1] + mine[0].end[1]) / 2)
    part, pad = min(
        ((p, q) for p in m.parts for q in p.pads if q.net == "GND" and q.smd),
        key=lambda pq: math.dist((pq[1].x, pq[1].y), mid),
    )
    ux, uy = pad.x - part.x, pad.y - part.y
    n = math.hypot(ux, uy) or 1.0
    # octilinear direction away from the part centre, 1 mm long
    ang = round(math.atan2(uy / n, ux / n) / (math.pi / 4)) * math.pi / 4
    dx, dy = round(math.cos(ang), 6), round(math.sin(ang), 6)
    ctx = placed.turn("run-rig-stub")
    made: list[str] = []

    async def run(sctx: BoardStageContext) -> None:
        made.extend(
            await sctx.call(
                lambda h: _stub_items(h, pad.x, pad.y, dx, dy), timeout=BOARD_READ_TIMEOUT_S
            )
        )

    cs = BoardChangeSet(
        tool="pcb_test_stub", logical=Logical(0), kind="board_fanout",
        board_stages=[BoardStage("stub", run)],
    )  # fmt: skip
    await apply_board_change_set(ctx, cs)
    return {"stub": made}


def _box(items: list[Any], grow: float = 0.3) -> Rect:
    pts = []
    for it in items:
        if hasattr(it, "at"):
            pts.append(it.at)
        else:
            pts += [it.start, it.end]
    x0, y0 = min(p[0] for p in pts) - grow, min(p[1] for p in pts) - grow
    x1, y1 = max(p[0] for p in pts) + grow, max(p[1] for p in pts) + grow
    return Rect(x0, y0, x1 - x0, y1 - y0)


def _unlocked_hash(m: BoardModel) -> str:
    return board_hash(
        dataclasses.replace(
            m,
            tracks=tuple(dataclasses.replace(t, locked=False) for t in m.tracks),
            vias=tuple(dataclasses.replace(v, locked=False) for v in m.vias),
        )
    )


# -- delete --------------------------------------------------------------------------------


async def test_unroute_net_deletes_unlocked(synth_placed: PlacedSynth, rig: dict[str, Any]) -> None:
    p = synth_placed
    await _ensure(p, "run-del-0", LOCKED, lock=True)
    out = await setup_call(p.turn("run-del-1"), "pcb_unroute", nets=[UNLOCKED, LOCKED])
    assert out["nets"] == [UNLOCKED] and out["removed_tracks"] > 0, out
    assert out["skipped_locked"] == 1 and out["skipped_locked_nets"] == [LOCKED], out
    m = await _model(p, "run-del-2")
    assert not _items(m, UNLOCKED)
    assert _items(m, LOCKED) and all(it.locked for it in _items(m, LOCKED))


async def test_unroute_include_locked(synth_placed: PlacedSynth, rig: dict[str, Any]) -> None:
    p = synth_placed
    await _ensure(p, "run-inc-0", LOCKED, lock=True)
    out = await setup_call(p.turn("run-inc-1"), "pcb_unroute", nets=[LOCKED], include_locked=True)
    assert out["nets"] == [LOCKED] and out["skipped_locked"] == 0, out
    assert not _items(await _model(p, "run-inc-2"), LOCKED)


async def test_unroute_region_whole_connection(
    synth_placed: PlacedSynth, rig: dict[str, Any]
) -> None:
    """A small region on the middle of one segment removes the whole connection; nothing
    dangles afterwards."""
    p = synth_placed
    await _ensure(p, "run-reg-0", UNLOCKED, lock=False)
    m = await _model(p, "run-reg-1")
    longest = max(
        (t for t in m.tracks if t.net == UNLOCKED), key=lambda t: math.dist(t.start, t.end)
    )
    mid = ((longest.start[0] + longest.end[0]) / 2, (longest.start[1] + longest.end[1]) / 2)
    region = {"x": mid[0] - 0.05, "y": mid[1] - 0.05, "w": 0.1, "h": 0.1}
    out = await setup_call(p.turn("run-reg-2"), "pcb_unroute", region=region)
    assert UNLOCKED in out["nets"], out
    after = await _model(p, "run-reg-3")
    assert not _items(after, UNLOCKED)
    ctx = p.turn("run-reg-4")
    drc: api.DrcResult = await ctx.session.call(
        lambda h: api.run_drc_task(h, timeout_s=60), timeout=api.drc_call_timeout(60)
    )
    assert not [mk for mk in drc.markers if mk.type == "dangling_track"], drc.markers


# -- unlock_only ---------------------------------------------------------------------------


async def test_unlock_only_returns_nets(synth_placed: PlacedSynth, rig: dict[str, Any]) -> None:
    p = synth_placed
    await _ensure(p, "run-ul-0", LOCKED, lock=True)
    before = await _model(p, "run-ul-1")
    ids = {it.kiid for it in _items(before, LOCKED)}
    out = await setup_call(p.turn("run-ul-2"), "pcb_unroute", nets=[LOCKED], unlock_only=True)
    assert out["unlocked_nets"] == [LOCKED] and out["unlocked"] == len(ids), out
    assert out["skipped_plane_items"] == 0
    after = await _model(p, "run-ul-3")
    assert {it.kiid for it in _items(after, LOCKED)} == ids
    assert not any(it.locked for it in _items(after, LOCKED))
    assert board_hash(after) != board_hash(before)
    assert _unlocked_hash(after) == _unlocked_hash(before)  # only the lock flags changed


async def test_unlock_only_skips_plane_net_in_region(
    synth_placed: PlacedSynth, rig: dict[str, Any]
) -> None:
    p = synth_placed
    await _ensure(p, "run-pl-0", LOCKED, lock=True)
    m = await _model(p, "run-pl-1")
    stub = [it for it in _items(m, "GND") if it.kiid in rig["stub"]]
    assert len(stub) == 2 and all(it.locked for it in stub)
    box = _box(_items(m, LOCKED) + stub)
    region = {"x": box.x, "y": box.y, "w": box.w, "h": box.h}
    out = await setup_call(p.turn("run-pl-2"), "pcb_unroute", region=region, unlock_only=True)
    assert LOCKED in out["unlocked_nets"] and "GND" not in out["unlocked_nets"], out
    assert out["skipped_plane_items"] == 2, out
    after = await _model(p, "run-pl-3")
    assert not any(it.locked for it in _items(after, LOCKED))
    assert all(it.locked for it in _items(after, "GND") if it.kiid in rig["stub"])


async def test_unroute_fail_after_1_restores_locked(
    synth_placed: PlacedSynth, rig: dict[str, Any]
) -> None:
    p = synth_placed
    await _ensure(p, "run-fa-0", LOCKED, lock=True)
    ctx = p.turn("run-fa-1")
    before = await board_model(ctx)
    cs = await route.prepare_unroute(ctx, {"nets": [LOCKED], "include_locked": True})
    assert isinstance(cs, BoardChangeSet) and cs.snapshot == "full"
    cs.fail_after = 1
    if ctx.on_mutation is not None:
        await ctx.on_mutation()
    with pytest.raises(RuntimeError, match="fail_after"):
        await route.apply_unroute(ctx, cs)
    after = await _model(p, "run-fa-2")
    assert board_hash(after) == board_hash(before)
    assert all(it.locked for it in _items(after, LOCKED))


async def test_unroute_budget_cost_1(synth_placed: PlacedSynth, rig: dict[str, Any]) -> None:
    p = synth_placed
    await _ensure(p, "run-cost-0", UNLOCKED, lock=False)
    out = await setup_call(p.turn("run-cost-1"), "pcb_unroute", nets=[UNLOCKED])
    assert out["logical_changes"] == 1 and out["budget"] == {"used": 1, "limit": 20}, out
    again = await setup_call(p.turn("run-cost-2"), "pcb_unroute", nets=[UNLOCKED])
    assert again["logical_changes"] == 0 and again["removed_tracks"] == 0, again


async def test_unroute_needs_selection(synth_placed: PlacedSynth) -> None:
    out, err = await call_tool(synth_placed.turn("run-sel"), "pcb_unroute")
    assert err and out["error"]["code"] == "invalid_argument", out
    out, err = await call_tool(synth_placed.turn("run-sel-2"), "pcb_unroute", nets=["/NOPE"])
    assert err and out["error"]["code"] == "not_found", out


async def test_unlock_only_plane_net_when_named(
    synth_placed: PlacedSynth, rig: dict[str, Any]
) -> None:
    """Last: unlocks the stub for good."""
    p = synth_placed
    out = await setup_call(p.turn("run-gnd-1"), "pcb_unroute", nets=["GND"], unlock_only=True)
    assert out["unlocked_nets"] == ["GND"] and out["unlocked"] == 2, out
    m = await _model(p, "run-gnd-2")
    assert not any(it.locked for it in _items(m, "GND") if it.kiid in rig["stub"])
