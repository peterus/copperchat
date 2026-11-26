"""`pcb_stitch` on the real api-server (SP4 P7 Task 4): GND stitching of the routed,
zoned and fanned-out `sp4_synth` (`copper_rig`), locked vias, dangling-via removal, the
edge ring, the via cap, the deadline / refill floor and the compensation of a failed call.

Tests that need a board without stitching vias first remove them (vias of the net no
track ends at); the module leaves the board stitched for the tidy-silk module."""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from copperchat.board.geom import rect_poly
from copperchat.board.jobs import strip_fills
from copperchat.board.model import BoardModel, board_hash
from copperchat.board.planes import _poly_dist, stitch_candidates
from copperchat.kicad_tools.board import api, stitch
from copperchat.kicad_tools.board.read import board_model
from copperchat.kicad_tools.board.route import class_params
from tests.kicad.board.conftest import (
    PlacedSynth,
    ensure_default_zones,
    ensure_fanout,
    setup_call,
)
from tests.support.edit import call_tool

pytestmark = [
    pytest.mark.kicad,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.timeout(900),  # the first test of a shard builds the session rig
]

NET = "GND"


async def _model(rig: PlacedSynth, run: str) -> BoardModel:
    return await board_model(rig.turn(run))


def _ids(m: BoardModel) -> dict[str, set[str]]:
    return {
        "zones": {z.kiid for z in m.zones},
        "tracks": {t.kiid for t in m.tracks},
        "vias": {v.kiid for v in m.vias},
    }


def _stitch_vias(m: BoardModel) -> list[Any]:
    ends = {pt for t in m.tracks if t.net == NET for pt in (t.start, t.end)}
    return [v for v in m.vias if v.net == NET and v.at not in ends]


async def _ready(rig: PlacedSynth, run: str) -> BoardModel:
    """Zones + fanout, no stitching vias."""
    await ensure_fanout(rig, NET)
    m = await _model(rig, f"{run}-r0")
    vias = _stitch_vias(m)
    if vias:
        ctx = rig.turn(f"{run}-r1")
        ids = sorted(v.kiid for v in vias)

        def remove(h: Any) -> None:
            from copperchat.kicad_tools.board.fanout import _kiids

            h.board().remove_items_by_id(_kiids(ids))

        async with ctx.session.commit("test: remove stitching", doc="pcb"):
            await ctx.session.call(remove, timeout=120)
        await ctx.session.call(
            lambda h: api.refill(h, timeout_s=60), timeout=api.refill_call_timeout(60)
        )
        m = await _model(rig, f"{run}-r2")
    return m


async def _apply(rig: PlacedSynth, run: str, **args: Any) -> dict[str, Any]:
    ctx = rig.turn(run)
    cs = await stitch.prepare_stitch(ctx, {"net": NET, **args})
    if ctx.on_mutation is not None:
        await ctx.on_mutation()
    result = await stitch.apply_stitch(ctx, cs)
    assert isinstance(result.data, dict)
    return result.data


async def _drc(rig: PlacedSynth) -> api.DrcResult:
    result: api.DrcResult = await rig.session.call(
        lambda h: api.run_drc_task(h, timeout_s=60), timeout=api.drc_call_timeout(60)
    )
    return result


# -- the grid ----------------------------------------------------------------------------


async def test_stitch_grid(copper_rig: PlacedSynth, record_property: Any) -> None:
    rig = copper_rig
    before = await _ready(rig, "run-grid")
    t0 = time.monotonic()
    out = await setup_call(rig.turn("run-grid-go"), "pcb_stitch", net=NET)
    record_property("stitch_wall_s", round(time.monotonic() - t0, 2))
    print({k: v for k, v in out.items() if k not in ("m11", "step_report")})
    for k in ("candidates", "kept", "rejected_drc", "removed_dangling", "seconds", "drc_seconds"):
        record_property(f"stitch_{k}", out[k])
    assert out["kept"] > 0 and out["next"] == "pcb_tidy_silk"
    assert out["kept"] + out["rejected_drc"] + out["removed_dangling"] == out["candidates"]
    assert out["m11"] is not None and out["m11"]["id"] == "M11"
    after = await _model(rig, "run-grid-after")
    new = [v for v in after.vias if v.kiid not in _ids(before)["vias"]]
    assert len(new) == out["kept"] and all(v.locked and v.net == NET for v in new)
    mine = {v.kiid for v in new}
    drc = await _drc(rig)
    for m in drc.markers:
        if m.severity == "exclusion" or not mine & set(m.kiids):
            continue
        assert m.type == "unconnected_items", m


async def test_edge_ring(copper_rig: PlacedSynth) -> None:
    rig = copper_rig
    await _ready(rig, "run-ring")
    plain = await _apply(rig, "run-ring-plain")
    await _ready(rig, "run-ring-2")
    ring = await _apply(rig, "run-ring-go", edge_ring=True)
    assert ring["kept"] > plain["kept"], (ring, plain)
    assert ring["edge_ring"] > 0 and ring["edge_ring_kept"] > 0
    m = await _model(rig, "run-ring-after")
    o = m.outline
    outline = rect_poly(o.x, o.y, o.w, o.h, o.corner_radius)
    inset = dict(m.rules)["copper_edge_clearance"] + 0.3 + 0.5
    near_edge = [v for v in _stitch_vias(m) if _poly_dist(v.at, outline) < inset + 0.5]
    assert len(near_edge) >= ring["edge_ring_kept"]
    for v in near_edge:
        assert abs(_poly_dist(v.at, outline) - inset) <= 0.5, v


async def test_stitch_cost_2(copper_rig: PlacedSynth) -> None:
    await _ready(copper_rig, "run-cost")
    cs = await stitch.prepare_stitch(copper_rig.turn("run-cost-go"), {"net": NET})
    assert cs.logical.changes == 2


async def test_stitch_candidates_via_run_cpu(
    copper_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _ready(copper_rig, "run-cpu")
    calls: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []
    real = stitch.run_cpu

    async def spy(fn: Any, *args: Any, **kw: Any) -> Any:
        calls.append((fn, args, kw))
        return await real(fn, *args, **kw)

    monkeypatch.setattr(stitch, "run_cpu", spy)
    await stitch.prepare_stitch(copper_rig.turn("run-cpu-go"), {"net": NET, "pitch": 4})
    assert [c[0] for c in calls] == [stitch.stitch_candidates_job]
    assert all(not z.filled for z in calls[0][1][0].zones)
    assert calls[0][2]["pitch"] == 4 and calls[0][2]["timeout_s"] == 30


async def test_stitch_too_many_vias(
    copper_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = copper_rig
    m = await _ready(rig, "run-many")
    p = class_params(m, NET)
    count = len(
        stitch_candidates(
            strip_fills(m), NET, via=p.via, clearance=p.clearance, pitch=1.0, edge_ring=True
        )
    )
    if count <= stitch.MAX_STITCH_VIAS:
        monkeypatch.setattr(stitch, "MAX_STITCH_VIAS", 5)
    cap = stitch.MAX_STITCH_VIAS
    out, err = await call_tool(
        rig.turn("run-many-go"), "pcb_stitch", net=NET, pitch=1, edge_ring=True
    )
    assert err and out["error"]["code"] == "invalid_argument", out
    assert out["error"]["details"] == {"candidates": count, "max": cap}
    assert board_hash(await _model(rig, "run-many-after")) == board_hash(m)


async def test_stitch_unknown_net(copper_rig: PlacedSynth) -> None:
    out, err = await call_tool(copper_rig.turn("run-unk"), "pcb_stitch", net="GNDD")
    assert err and out["error"]["code"] == "not_found", out


# -- compensation ----------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 2, 3])
async def test_stitch_fail_after(copper_rig: PlacedSynth, n: int) -> None:
    """After the add (1), after the pushed refill (2), after DRC + removal (3): KIID sets and
    board_hash equal pre-call."""
    rig = copper_rig
    await _ready(rig, f"run-fa{n}")
    ctx = rig.turn(f"run-fa{n}-go")
    before = await board_model(ctx)
    cs = await stitch.prepare_stitch(ctx, {"net": NET})
    cs.fail_after = n
    if ctx.on_mutation is not None:
        await ctx.on_mutation()
    with pytest.raises(RuntimeError, match="fail_after"):
        await stitch.apply_stitch(ctx, cs)
    after = await _model(rig, f"run-fa{n}-after")
    assert _ids(after) == _ids(before)
    assert board_hash(after) == board_hash(before)


async def test_stitch_refill_pushed_before_drc(
    copper_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = copper_rig
    await _ready(rig, "run-order")
    session = rig.session
    events: list[str] = []
    depth = [0]
    real_commit = session.commit

    @contextlib.asynccontextmanager
    async def commit(label: str, **kw: Any) -> AsyncIterator[Any]:
        events.append("commit")
        depth[0] += 1
        try:
            async with real_commit(label, **kw) as c:
                yield c
        finally:
            depth[0] -= 1

    real_refill, real_drc = api.refill, api.run_drc_task

    def refill(h: Any, *, timeout_s: float) -> None:
        events.append(f"refill@{depth[0]}")
        real_refill(h, timeout_s=timeout_s)

    def drc(h: Any, *, timeout_s: float = 60.0) -> Any:
        events.append(f"drc@{depth[0]}")
        return real_drc(h, timeout_s=timeout_s)

    monkeypatch.setattr(session, "commit", commit)
    monkeypatch.setattr(api, "refill", refill)
    monkeypatch.setattr(api, "run_drc_task", drc)
    await _apply(rig, "run-order-go")
    monkeypatch.undo()
    i = events.index("drc@0")
    assert events[0] == "commit" and events[1] == "refill@0" and i > 1, events
    assert not [e for e in events if e.startswith("drc") and e != "drc@0"]


# -- deadline --------------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.frozen = 1000.0

    def __call__(self) -> float:
        return self.frozen


def _spy(monkeypatch: pytest.MonkeyPatch, session: Any) -> list[tuple[str, Any]]:
    log: list[tuple[str, Any]] = []
    real_refill, real_drc, real_call = api.refill, api.run_drc_task, session.call

    def refill(h: Any, *, timeout_s: float) -> None:
        log.append(("refill", timeout_s))
        real_refill(h, timeout_s=timeout_s)

    def drc(h: Any, *, timeout_s: float = 60.0) -> Any:
        log.append(("drc", timeout_s))
        return real_drc(h, timeout_s=timeout_s)

    async def call(fn: Any, *, timeout: float | None = None) -> Any:
        def wrapped(h: Any) -> Any:
            n = len(log)
            try:
                return fn(h)
            finally:
                if any(e[0] in ("refill", "drc") for e in log[n:]):
                    log.append(("call", timeout))

        return await real_call(wrapped, timeout=timeout)

    monkeypatch.setattr(api, "refill", refill)
    monkeypatch.setattr(api, "run_drc_task", drc)
    monkeypatch.setattr(session, "call", call)
    return log


@pytest.mark.parametrize("left", [-1.0, 19.0])
async def test_stitch_deadline(
    copper_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch, left: float
) -> None:
    """The deadline passed before stage 3 (`left` -1 after the stage-2 refill) or 19 s left
    before stage 2 (no refill started): no DRC, every stage-1 via removed as
    `rejected_timeout`, the board's KIID set as before; stage 4's cleanup refill gets
    REFILL_TIMEOUT_S (call timeout 105)."""
    rig = copper_rig
    before = await _ready(rig, f"run-dl{left:g}")
    clock = _Clock()
    monkeypatch.setattr(stitch, "monotonic", clock)
    ctx = rig.turn(f"run-dl{left:g}-go")
    cs = await stitch.prepare_stitch(ctx, {"net": NET})
    st = cs.state  # type: ignore[attr-defined]
    if left < 0:  # the deadline passes during the stage-2 refill
        real_refill = api.refill

        def late(h: Any, *, timeout_s: float) -> None:
            real_refill(h, timeout_s=timeout_s)
            clock.frozen = st.deadline - left

        monkeypatch.setattr(api, "refill", late)
    else:
        clock.frozen = st.deadline - left
    log = _spy(monkeypatch, rig.session)
    if ctx.on_mutation is not None:
        await ctx.on_mutation()
    result = await stitch.apply_stitch(ctx, cs)
    monkeypatch.undo()
    data = result.data
    assert isinstance(data, dict)
    assert not [e for e in log if e[0] == "drc"]
    assert data["kept"] == 0 and data["rejected_timeout"] == data["candidates"] > 0
    refills = [e for e in log if e[0] == "refill"]
    assert refills[-1] == ("refill", api.REFILL_TIMEOUT_S)
    assert log[log.index(refills[-1]) + 1] == ("call", 105)
    assert len(refills) == (2 if left < 0 else 1)
    after = await _model(rig, f"run-dl{left:g}-after")
    assert _ids(after) == _ids(before)


async def test_stitch_without_two_zone_layers(copper_rig: PlacedSynth) -> None:
    """Only the In1.Cu plane: nothing to stitch (kept 0, a note, cost 0)."""
    rig = copper_rig
    await _ready(rig, "run-two")
    await setup_call(
        rig.turn("run-two-del"),
        "pcb_zones",
        action="delete",
        zones=[{"name": "GND_F"}, {"name": "GND_B"}],
    )
    try:
        out = await setup_call(rig.turn("run-two-go"), "pcb_stitch", net=NET)
        assert out["kept"] == 0 and out["candidates"] == 0 and "note" in out
        assert out["logical_changes"] == 0
    finally:
        await ensure_default_zones(rig)


async def test_zz_stitch_leaves_board_stitched(copper_rig: PlacedSynth) -> None:
    """Last in the module: the board is stitched again for the tidy-silk module."""
    rig = copper_rig
    m = await _model(rig, "run-final")
    if not _stitch_vias(m):
        await setup_call(rig.turn("run-final-go"), "pcb_stitch", net=NET)
    assert _stitch_vias(await _model(rig, "run-final-2"))
