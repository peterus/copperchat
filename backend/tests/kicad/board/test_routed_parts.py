"""The routed-part rule on the real api-server (SP4 P4 Task 4, spec §7.2 "Moves on a routed
board", call 66): `sp4_synth` set up (`synth_setup`), every part placed in a grid, then -
through board change sets of this test (`apply_board_change_set`, the API only) - a
**locked** two-segment octilinear track between U1 and C1 on their shared supply net and a
**locked** fanout-like stub + via on U1's GND pad. Moving a routed part is refused
(`part_routed`) unless `unroute: true`, which removes both components (locked included) and
reports their nets as unrouted; a failure after the unroute stage re-creates them."""

from __future__ import annotations

import itertools
import json
import math
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from kipy.board_types import Track, Via
from kipy.geometry import Vector2
from kipy.proto.board.board_types_pb2 import BoardLayer

from copperchat.board.model import BoardModel, Pad, board_hash, placement_hash
from copperchat.db import Database
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.board import api, place
from copperchat.kicad_tools.board.engine import (
    BoardChangeSet,
    BoardStage,
    BoardStageContext,
    apply_board_change_set,
)
from copperchat.kicad_tools.board.read import board_model
from copperchat.kicad_tools.edit.changeset import Logical
from copperchat.kicad_tools.edit.engine import save_after_edit
from copperchat.kicad_tools.units import mm_to_nm
from copperchat.tools.spec import ToolContext
from tests.kicad.board.conftest import SynthSetup, analyzed_blocks, open_setup_db, setup_call
from tests.kicad.board.test_approve import grid_moves
from tests.support.edit import call_tool

pytestmark = [pytest.mark.kicad, pytest.mark.asyncio(loop_scope="module")]

STUB_MM = 0.6  # beyond the pad edge
VIA = (0.6, 0.3)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def placed(
    synth_setup: SynthSetup, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[SynthSetup]:
    tmp = tmp_path_factory.mktemp("routed-placed")
    db = await open_setup_db(tmp / "placed.db", synth_setup.session.project.id)
    try:
        ctx = synth_setup.turn(db, "run-grid")
        moves = grid_moves(await board_model(ctx))
        items = [{"ref": r, "x": x, "y": y} for r, (x, y) in sorted(moves.items())]
        await setup_call(ctx, "pcb_move", items=items)
        yield synth_setup
    finally:
        await db.close()


def _shared(m: BoardModel) -> tuple[Pad, Pad, Pad]:
    """(U1 pad, C1 pad) on their shared non-ground net, and U1's GND pad."""
    u1, c1 = m.part("U1"), m.part("C1")
    c1_nets = {q.net: q for q in c1.pads if q.net and q.net != "GND"}
    up = next(q for q in sorted(u1.pads, key=lambda q: q.number) if q.net in c1_nets)
    gp = next(q for q in sorted(u1.pads, key=lambda q: q.number) if q.net == "GND")
    return up, c1_nets[up.net], gp


def _octilinear(a: tuple[float, float], b: tuple[float, float]) -> list[tuple[float, float]]:
    dx, dy = b[0] - a[0], b[1] - a[1]
    if abs(dx) >= abs(dy):
        mid = (a[0] + dx - math.copysign(abs(dy), dx), a[1])
    else:
        mid = (a[0], a[1] + dy - math.copysign(abs(dx), dy))
    return [a, mid, b]


def _vec(p: tuple[float, float]) -> Vector2:
    return Vector2.from_xy(mm_to_nm(p[0]), mm_to_nm(p[1]))


def _net(h: KicadHandle, name: str) -> Any:
    return next(n for n in h.board().get_nets() if n.name == name)


def _tracks(h: KicadHandle, pts: list[tuple[float, float]], net: str) -> list[str]:
    made = []
    for a, b in itertools.pairwise(pts):
        t = Track()
        t.layer = BoardLayer.BL_F_Cu
        t.net = _net(h, net)
        t.start, t.end = _vec(a), _vec(b)
        t.width = 200_000
        t.locked = True
        made.append(t)
    return [str(c.id.value) for c in h.board().create_items(made)]


def _via(h: KicadHandle, at: tuple[float, float], net: str) -> str:
    v = Via()
    v.position = _vec(at)
    v.net = _net(h, net)
    layer = v.padstack.proto.copper_layers.add()
    layer.layer = BoardLayer.BL_F_Cu
    v.diameter = mm_to_nm(VIA[0])
    v.drill_diameter = mm_to_nm(VIA[1])
    v.locked = True
    (c,) = h.board().create_items([v])
    return str(c.id.value)


async def add_copper(ctx: ToolContext) -> dict[str, Any]:
    """The locked route (stage 1) and the locked GND stub + via (stage 2) through one board
    change set, saved; returns their KIIDs and nets."""
    m = await board_model(ctx)
    up, cp, gp = _shared(m)
    u1 = m.part("U1")
    route = _octilinear((up.x, up.y), (cp.x, cp.y))
    out = 1.0 if gp.x >= u1.x else -1.0
    stub_end = (gp.x + out * (max(gp.w, gp.h) / 2 + STUB_MM), gp.y)
    made: dict[str, Any] = {"nets": sorted({up.net, "GND"})}

    async def s1(sc: BoardStageContext) -> None:
        made["route"] = await sc.call(lambda h: _tracks(h, route, up.net), timeout=60)

    async def s2(sc: BoardStageContext) -> None:
        stub = await sc.call(lambda h: _tracks(h, [(gp.x, gp.y), stub_end], "GND"), timeout=60)
        via = await sc.call(lambda h: _via(h, stub_end, "GND"), timeout=60)
        made["fanout"] = [*stub, via]

    cs = BoardChangeSet(
        tool="pcb_route",
        kind="board_route",
        board_stages=[BoardStage("route", s1), BoardStage("fanout", s2)],
        logical=Logical(1),
    )
    if ctx.on_mutation is not None:
        await ctx.on_mutation()
    await apply_board_change_set(ctx, cs)
    await save_after_edit(ctx, {"pcb"})
    m = await board_model(ctx)
    locked = {t.kiid: t.locked for t in m.tracks} | {v.kiid: v.locked for v in m.vias}
    assert all(locked[k] for k in made["route"] + made["fanout"]), locked
    return made


async def _hashes(ctx: ToolContext) -> tuple[str, str]:
    m = await board_model(ctx)
    return placement_hash(m), board_hash(m)


def _copper_ids(m: BoardModel) -> set[str]:
    return {t.kiid for t in m.tracks} | {a.kiid for a in m.arcs} | {v.kiid for v in m.vias}


async def _refused(ctx: ToolContext, name: str, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx, name, **args)
    assert err, out
    error: dict[str, Any] = out["error"]
    return error


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def copper(
    placed: SynthSetup, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[dict[str, Any]]:
    tmp = tmp_path_factory.mktemp("routed-copper")
    db = await open_setup_db(tmp / "copper.db", placed.session.project.id)
    try:
        yield await add_copper(placed.turn(db, "run-copper"))
    finally:
        await db.close()


async def test_move_routed_refused(
    placed: SynthSetup, copper: dict[str, Any], setup_db: Database
) -> None:
    ctx = placed.turn(setup_db, "run-r1")
    before = await _hashes(ctx)
    u1 = (await board_model(ctx)).part("U1")
    error = await _refused(ctx, "pcb_move", items=[{"ref": "U1", "x": u1.x + 1}])
    assert error["code"] == "part_routed", error
    assert error["details"]["refs"] == ["U1"]
    assert error["details"]["nets"] == copper["nets"]
    assert await _hashes(ctx) == before
    # a move below placement_hash's rounding is no move: allowed without unroute
    out = await setup_call(ctx, "pcb_move", items=[{"ref": "U1", "x": u1.x + 0.004}])
    assert out["unrouted_nets"] == []
    await setup_call(ctx, "pcb_move", items=[{"ref": "U1", "x": u1.x}])


async def test_unroute_move_fail_after_2(
    placed: SynthSetup,
    copper: dict[str, Any],
    setup_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = placed.turn(setup_db, "run-r2")
    before = await _hashes(ctx)
    ids = _copper_ids(await board_model(ctx))
    real = place.apply_board_change_set

    async def failing(ctx: ToolContext, cs: Any) -> Any:
        assert cs.snapshot == "full" and len(cs.board_stages) == 2
        cs.fail_after = 2
        return await real(ctx, cs)

    monkeypatch.setattr(place, "apply_board_change_set", failing)
    u1 = (await board_model(ctx)).part("U1")
    out, err = await call_tool(ctx, "pcb_move", items=[{"ref": "U1", "x": u1.x + 1}], unroute=True)
    assert err, out
    assert out["error"].get("code") != "document_inconsistent", out
    m = await board_model(ctx)
    assert (placement_hash(m), board_hash(m)) == before  # locked copper re-created, locked
    assert _copper_ids(m) == ids


async def test_move_unroute_removes_locked(
    placed: SynthSetup, copper: dict[str, Any], setup_db: Database
) -> None:
    ctx = placed.turn(setup_db, "run-r3")
    u1 = (await board_model(ctx)).part("U1")
    out = await setup_call(ctx, "pcb_move", items=[{"ref": "U1", "x": u1.x + 1}], unroute=True)
    m = await board_model(ctx)
    gone = set(copper["route"]) | set(copper["fanout"])
    assert not gone & _copper_ids(m)
    assert out["unrouted_nets"] == copper["nets"]
    assert out["moved"] == ["U1"]
    assert m.part("U1").x == pytest.approx(u1.x + 1, abs=1e-3)
    drc = await ctx.session.call(lambda h: api.run_drc_task(h), timeout=api.drc_call_timeout(60))
    dangling = [mk for mk in drc.markers if mk.type in ("dangling_track", "dangling_via")]
    assert dangling == [], dangling
    print("change log:", json.dumps([s["stage"] for s in out["step_report"]]))
    assert [s["stage"] for s in out["step_report"]] == ["unroute", "move"]
    await setup_call(ctx, "pcb_move", items=[{"ref": "U1", "x": u1.x}])


async def test_place_block_routed_refused(placed: SynthSetup, setup_db: Database) -> None:
    ctx = placed.turn(setup_db, "run-r4")
    made = await add_copper(ctx)
    blocks = await analyzed_blocks(ctx)
    await setup_call(ctx, "pcb_define_blocks", blocks=blocks, replace=True)
    name = next(b["name"] for b in blocks if "U1" in b["refs"])
    before = await _hashes(ctx)
    error = await _refused(
        ctx, "pcb_place_block", block=name, region={"x": 122, "y": 104, "w": 34, "h": 37}
    )
    assert error["code"] == "part_routed", error
    assert "U1" in error["details"]["refs"]
    assert set(made["nets"]) <= set(error["details"]["nets"])
    assert await _hashes(ctx) == before


async def test_legalize_routed_refused(placed: SynthSetup, setup_db: Database) -> None:
    """A locked part dropped onto the routed U1: legalising must move U1 → refused."""
    ctx = placed.turn(setup_db, "run-r5")
    m = await board_model(ctx)
    assert m.tracks, "the copper of the previous test is still there"
    u1, r1 = m.part("U1"), m.part("R1")
    await setup_call(ctx, "pcb_move", items=[{"ref": "R1", "x": u1.x, "y": u1.y, "locked": True}])
    before = await _hashes(ctx)
    error = await _refused(ctx, "pcb_legalize", scope="all")
    assert error["code"] == "part_routed", error
    assert "U1" in error["details"]["refs"]
    assert await _hashes(ctx) == before
    out = await setup_call(ctx, "pcb_legalize", scope="all", unroute=True)
    assert "U1" in out["moved"] and out["unrouted_nets"]
    await setup_call(ctx, "pcb_move", items=[{"ref": "R1", "x": r1.x, "y": r1.y, "locked": False}])
