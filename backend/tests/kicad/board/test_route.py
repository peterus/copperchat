"""`pcb_route` on the real api-server (SP4 P5b Task 2): the critical set of `sp4_synth`
routed by `routed_synth` (DRC-verified, locked, octilinear, the placement approval still
valid), the refusals, the budget and the compensation paths.

All tests share one module server (`synth_routed`, ≈ 1.9 GB); a test that needs a net
unrouted first removes its copper with `pcb_unroute {include_locked: true}` (the placement
and its approval stay)."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from copperchat.board.model import BoardModel, Pad, board_hash, placement_hash
from copperchat.board.router import RoutePath
from copperchat.kicad_tools.board import api, route
from copperchat.kicad_tools.board.read import board_model
from copperchat.projects import agent_dir
from tests.kicad.board.conftest import PlacedSynth, setup_call
from tests.support.edit import call_tool

pytestmark = [pytest.mark.kicad, pytest.mark.asyncio(loop_scope="module")]

PAIR = ("/USB_DP", "/USB_DN")
SIGNALS = ("/SWDIO", "/SWCLK")  # short 2-pin nets (critical on the placed board)


async def _model(placed: PlacedSynth, run: str) -> BoardModel:
    return await board_model(placed.turn(run))


async def _drc(placed: PlacedSynth, run: str) -> api.DrcResult:
    ctx = placed.turn(run)
    result: api.DrcResult = await ctx.session.call(
        lambda h: api.run_drc_task(h, timeout_s=60), timeout=api.drc_call_timeout(60)
    )
    return result


def _errors(drc: api.DrcResult) -> list[api.DrcMarker]:
    return [m for m in drc.markers if m.severity == "error" and m.type != "unconnected_items"]


async def _unroute(placed: PlacedSynth, run: str, nets: list[str]) -> None:
    await setup_call(placed.turn(run), "pcb_unroute", nets=nets, include_locked=True)


def _copper_ids(m: BoardModel) -> set[str]:
    return {t.kiid for t in m.tracks} | {a.kiid for a in m.arcs} | {v.kiid for v in m.vias}


# -- the critical set ----------------------------------------------------------------------


async def test_route_critical_set(synth_routed: PlacedSynth, record_property: Any) -> None:
    placed = synth_routed
    info = placed.info
    nets = info["critical_nets"]
    assert nets and not any("GND" in n for n in nets)
    entries = [e for chunk in info["routed"] for e in chunk["nets"]]
    assert [e["net"] for e in entries] == nets
    for chunk in info["routed"]:
        print("totals", chunk["totals"], chunk["bends"], chunk["drc_errors"], chunk["seconds"])
        assert chunk["bends"]["M4"] == 0 and chunk["bends"]["M7"] == 0
        assert chunk["bends"]["M15"] == 0
        record_property("route_seconds", chunk["seconds"])
    for e in entries:
        print(e["net"], e["status"], e["routed"], "/", e["connections"], e["seconds"], e["failed"])
        record_property(f"route_s {e['net']}", e["seconds"])
    render = await setup_call(
        placed.turn("run-crit-render"), "pcb_render", view="top", highlight_nets=nets[:20]
    )
    out = os.environ.get("P5B_RENDER_DIR")
    if out:  # local runs keep the render (handed to the owner)
        name = render["png_url"].rsplit("/", 1)[-1]
        src = Path(agent_dir(placed.session.project)) / "renders" / name
        shutil.copy(src, Path(out) / "sp4_synth_routed_critical_top.png")
    failed = [(e["net"], f) for e in entries for f in e.get("failed", ())]
    assert failed == [], failed
    assert all(e["status"] in ("routed", "already_routed") for e in entries)
    drc = await _drc(placed, "run-crit-drc")
    assert _errors(drc) == [], _errors(drc)
    metrics = await setup_call(placed.turn("run-crit-m"), "pcb_metrics", only=["M4", "M7"])
    for m in metrics["metrics"]:
        assert m["value"] == 0 and m["verdict"] == "pass", m
    model = await _model(placed, "run-crit-model")
    mine = [t for t in model.tracks if t.net in nets] + [v for v in model.vias if v.net in nets]
    assert mine and all(it.locked for it in mine)
    assert placement_hash(model) == info["placement_hash"]
    status = await setup_call(placed.turn("run-crit-status"), "pcb_status")
    assert status["checkpoints"]["placement"]["approved"]
    assert not status["checkpoints"]["placement"]["stale"]


async def test_pair_routed_coupled(synth_routed: PlacedSynth) -> None:
    entries = {e["net"]: e for chunk in synth_routed.info["routed"] for e in chunk["nets"]}
    for net in PAIR:
        e = entries[net]
        assert e["status"] == "routed", e
        coupling = e.get("coupling")
        print(net, coupling)
        assert coupling is not None and set(coupling) == {"length_mismatch", "gap_min", "gap_max"}
    model = await _model(synth_routed, "run-pair")
    layers = [{t.layer for t in model.tracks if t.net == n} for n in PAIR]
    assert layers[0] == layers[1], layers
    mismatch = entries[PAIR[0]]["coupling"]["length_mismatch"]
    (pair,) = [p for chunk in synth_routed.info["routed"] for p in chunk["pairs"]]
    print(pair)
    assert (pair["p"], pair["n"]) == PAIR
    # over 1 mm (M12's pair tolerance) the report lists it with a warning for the agent
    assert mismatch <= 1.0 or "warning" in pair, pair


# -- options ---------------------------------------------------------------------------


async def test_lock_false(synth_routed: PlacedSynth) -> None:
    net = synth_routed.info["critical_nets"][0]
    await _unroute(synth_routed, "run-lock-0", [net])
    out = await setup_call(synth_routed.turn("run-lock-1"), "pcb_route", nets=[net], lock=False)
    (e,) = out["nets"]
    assert e["status"] == "routed" and e["locked"] is False, e
    model = await _model(synth_routed, "run-lock-2")
    items = [t for t in model.tracks if t.net == net] + [v for v in model.vias if v.net == net]
    assert items and not any(it.locked for it in items)
    await _unroute(synth_routed, "run-lock-3", [net])  # back to locked for later tests
    await setup_call(synth_routed.turn("run-lock-4"), "pcb_route", nets=[net])


async def test_no_vias_when_disallowed(synth_routed: PlacedSynth) -> None:
    await _unroute(synth_routed, "run-novia-0", list(SIGNALS))  # each test alone on a shard
    nets = list(SIGNALS)
    out = await setup_call(
        synth_routed.turn("run-novia-1"), "pcb_route", nets=nets, allow_vias=False
    )
    print(out["nets"])
    model = await _model(synth_routed, "run-novia-2")
    assert not [v for v in model.vias if v.net in nets]
    assert all(e["vias"] == 0 for e in out["nets"] if e["status"] != "already_routed")
    await _unroute(synth_routed, "run-novia-3", nets)


# -- refusals ------------------------------------------------------------------------------


async def test_placement_not_approved_refusal(synth_routed: PlacedSynth, tmp_path: Path) -> None:
    """No approval (a database without the checkpoint) → refused, `stale` false; after a
    0.5 mm move of a part without copper (a mounting hole) → refused, `stale` true; moved
    back exactly → the approval holds again."""
    from tests.kicad.board.conftest import open_setup_db

    fresh = await open_setup_db(tmp_path / "fresh.db", synth_routed.session.project.id)
    try:
        ctx = synth_routed.rig.turn(fresh, "run-na-1")
        out, err = await call_tool(ctx, "pcb_route", nets=list(SIGNALS[:1]))
        assert err and out["error"]["code"] == "placement_not_approved", out
        assert out["error"]["details"]["stale"] is False
    finally:
        await fresh.close()
    model = await _model(synth_routed, "run-na-2")
    hole = next(p for p in model.parts if p.ref.startswith("H"))
    await setup_call(
        synth_routed.turn("run-na-3"), "pcb_move", items=[{"ref": hole.ref, "x": hole.x + 0.5}]
    )
    out, err = await call_tool(synth_routed.turn("run-na-4"), "pcb_route", nets=[SIGNALS[0]])
    assert (
        err
        and out["error"]["code"] == "placement_not_approved"
        and out["error"]["details"]["stale"] is True
    )
    await setup_call(
        synth_routed.turn("run-na-5"), "pcb_move", items=[{"ref": hole.ref, "x": hole.x}]
    )
    assert (
        placement_hash(await _model(synth_routed, "run-na-6"))
        == (synth_routed.info["placement_hash"])
    )


async def test_unknown_net_closest(synth_routed: PlacedSynth) -> None:
    out, err = await call_tool(synth_routed.turn("run-unk"), "pcb_route", nets=["/USB_DQ"])
    assert err and out["error"]["code"] == "not_found", out
    assert "/USB_DP" in out["error"]["details"]["closest"]["/USB_DQ"]


async def test_unknown_layer(synth_routed: PlacedSynth) -> None:
    out, err = await call_tool(
        synth_routed.turn("run-unk-l"), "pcb_route", nets=[SIGNALS[0]], layers=["In7.Cu"]
    )
    assert err and out["error"]["code"] == "not_found", out


async def test_ground_net_refused(synth_routed: PlacedSynth) -> None:
    out, err = await call_tool(synth_routed.turn("run-gnd"), "pcb_route", nets=["GND"])
    assert err and out["error"]["code"] == "invalid_argument", out


async def test_already_routed_reported(synth_routed: PlacedSynth) -> None:
    net = synth_routed.info["critical_nets"][0]
    out = await setup_call(synth_routed.turn("run-already"), "pcb_route", nets=[net])
    assert out["nets"] == [{"net": net, "status": "already_routed", "connections": 0}]
    assert out["logical_changes"] == 0


async def test_budget_cost(synth_routed: PlacedSynth) -> None:
    """6 nets → ceil(6 / 5) = 2 (the plan only; nothing applied)."""
    model = await _model(synth_routed, "run-cost-0")
    pads = sorted({q.net for p in model.parts for q in p.pads if q.net})
    routed = {t.net for t in model.tracks}
    unrouted = [n for n in pads if n not in routed and "GND" not in n and "unconnected" not in n]
    nets = (unrouted + synth_routed.info["critical_nets"])[:6]  # routed ones cost the same
    assert len(nets) == 6 and unrouted, nets
    cs = await route.prepare_route(synth_routed.turn("run-cost"), {"nets": nets})
    assert cs.logical.changes == 2


# -- compensation --------------------------------------------------------------------------


async def test_route_fail_after_1(synth_routed: PlacedSynth) -> None:
    await _unroute(synth_routed, "run-fail-0", list(SIGNALS))  # each test alone on a shard
    ctx = synth_routed.turn("run-fail")
    before = await board_model(ctx)
    cs = await route.prepare_route(ctx, {"nets": list(SIGNALS)})
    assert len(cs.board_stages) == 1  # one stage pushing a commit per connection
    cs.fail_after = 1  # raise after it: both nets' copper was pushed
    if ctx.on_mutation is not None:
        await ctx.on_mutation()
    with pytest.raises(RuntimeError, match="fail_after"):
        await route.apply_route(ctx, cs)
    after = await board_model(synth_routed.turn("run-fail-2"))
    assert board_hash(after) == board_hash(before)
    assert _copper_ids(after) == _copper_ids(before)


async def test_route_cancel_compensates(
    synth_routed: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _unroute(synth_routed, "run-cancel-0", list(SIGNALS))  # each test alone on a shard
    ctx = synth_routed.turn("run-cancel")
    before = await board_model(ctx)
    cs = await route.prepare_route(ctx, {"nets": list(SIGNALS)})
    second = SIGNALS[1]  # the first net's copper is pushed before this one solves
    real = route.run_cpu
    waiting = asyncio.Event()

    async def blocking(fn: Any, *args: Any, **kw: Any) -> Any:
        req = args[1]
        if req.net == second:
            waiting.set()
            await asyncio.Event().wait()  # until cancelled
        return await real(fn, *args, **kw)

    monkeypatch.setattr(route, "run_cpu", blocking)
    if ctx.on_mutation is not None:
        await ctx.on_mutation()
    task = asyncio.create_task(route.apply_route(ctx, cs))
    await asyncio.wait_for(waiting.wait(), 120)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    after = await board_model(synth_routed.turn("run-cancel-2"))
    assert board_hash(after) == board_hash(before)
    assert placement_hash(after) == placement_hash(before)
    assert _copper_ids(after) == _copper_ids(before)


def _short_path(model: BoardModel, a: Pad) -> RoutePath:
    """An octilinear stub from `a` straight into the nearest other-net pad on its layer
    (a short circuit the DRC task rejects)."""
    layer = "F.Cu"
    other = min(
        (
            q
            for p in model.parts
            for q in p.pads
            if q.net and q.net != a.net and layer in q.layers and abs(q.y - a.y) < 1e-6
        ),
        key=lambda q: abs(q.x - a.x),
        default=None,
    )
    if other is None:  # none on the same row: go straight through the closest pad's row
        other = min(
            (q for p in model.parts for q in p.pads if q.net and q.net != a.net),
            key=lambda q: abs(q.x - a.x) + abs(q.y - a.y),
        )
        mid = (other.x, a.y)
        return RoutePath(
            ((layer, (a.x, a.y), mid, 0.25), (layer, mid, (other.x, other.y), 0.25)), (), 0.0
        )
    return RoutePath(((layer, (a.x, a.y), (other.x, other.y), 0.25),), (), abs(other.x - a.x))


async def test_drc_reject_then_retry(
    synth_routed: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first answer of `run_cpu` is a hand-made path shorting another net's pad (only
    CopperChat code is patched); the real DRC task rejects it, its items are removed, the
    retry (margin 0.05, the real router) is accepted, nothing of the rejected try is left."""
    await _unroute(synth_routed, "run-rej-0", list(SIGNALS))  # each test alone on a shard
    net = SIGNALS[0]
    ctx = synth_routed.turn("run-rej")
    model = await board_model(ctx)
    real = route.run_cpu
    calls: list[float] = []

    async def first_bad(fn: Any, *args: Any, **kw: Any) -> Any:
        calls.append(kw.get("margin", 0.0))
        if len(calls) == 1:
            req = args[1]
            return _short_path(model, req.a)
        return await real(fn, *args, **kw)

    monkeypatch.setattr(route, "run_cpu", first_bad)
    before = _copper_ids(model)
    out = await setup_call(ctx, "pcb_route", nets=[net])
    (e,) = out["nets"]
    assert e["status"] == "routed", e
    assert calls[:2] == [0.0, 0.05]
    after = await board_model(synth_routed.turn("run-rej-2"))
    new = _copper_ids(after) - before
    assert new and all(t.net == net for t in after.tracks if t.kiid in new)
    assert _errors(await _drc(synth_routed, "run-rej-3")) == []
    await _unroute(synth_routed, "run-rej-4", [net])
