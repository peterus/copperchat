"""`pcb_fanout` on the real api-server (SP4 P7 Task 3): GND fanout of the routed
`sp4_synth` with the default zones (`copper_rig`), locked DRC-legalised stubs + vias (no
via-in-pad), the round / deadline / refill rules and the compensation of a failed call.

Tests that need open pads first remove the GND copper (`pcb_unroute {nets: [GND],
include_locked: true}`); the module leaves the board fanned out for the later P7 modules."""

from __future__ import annotations

import contextlib
import math
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from copperchat.board.geom import analyse_bends
from copperchat.board.model import BoardModel, board_hash
from copperchat.board.planes import _point_pad, fanout_pads
from copperchat.kicad_tools.board import api, fanout
from copperchat.kicad_tools.board.read import board_model
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


async def _ready(rig: PlacedSynth, run: str) -> BoardModel:
    """Default zones poured and no GND copper: every GND SMD pad open again."""
    await ensure_default_zones(rig)
    m = await _model(rig, f"{run}-r0")
    if any(t.net == NET for t in m.tracks) or any(v.net == NET for v in m.vias):
        await setup_call(rig.turn(f"{run}-r1"), "pcb_unroute", nets=[NET], include_locked=True)
        m = await _model(rig, f"{run}-r2")
    return m


async def _apply(rig: PlacedSynth, run: str, **args: Any) -> dict[str, Any]:
    ctx = rig.turn(run)
    cs = await fanout.prepare_fanout(ctx, {"net": NET, **args})
    if ctx.on_mutation is not None:
        await ctx.on_mutation()
    result = await fanout.apply_fanout(ctx, cs)
    assert isinstance(result.data, dict)
    return result.data


class _Clock:
    """`fanout.monotonic` stand-in: real time + `offset`, or frozen at `frozen`."""

    def __init__(self) -> None:
        self.offset = 0.0
        self.frozen: float | None = None

    def __call__(self) -> float:
        return self.frozen if self.frozen is not None else time.monotonic() + self.offset


# -- the GND fanout --------------------------------------------------------------------------


async def test_fanout_gnd(copper_rig: PlacedSynth, record_property: Any) -> None:
    rig = copper_rig
    before = await _ready(rig, "run-gnd")
    todo, done = fanout_pads(before, NET)
    t0 = time.monotonic()
    out = await setup_call(rig.turn("run-gnd-go"), "pcb_fanout", net=NET)
    seconds = time.monotonic() - t0
    print({k: v for k, v in out.items() if k != "m11"})
    for k in ("smd_pads", "fanned_out", "via_pour", "rounds", "drc_seconds", "seconds"):
        record_property(f"fanout_{k}", out[k])
    record_property("fanout_wall_s", round(seconds, 2))
    assert out["smd_pads"] == len(todo) + len(done)
    assert out["fanned_out"] + out["via_pour"] + len(out["unconnected"]) == len(todo)
    assert out["fanned_out"] >= 1
    assert out["m11"] is not None and out["m11"]["id"] == "M11"
    assert out["next"] == "pcb_stitch"
    after = await _model(rig, "run-gnd-after")
    old = _ids(before)
    new_tracks = [t for t in after.tracks if t.kiid not in old["tracks"]]
    new_vias = [v for v in after.vias if v.kiid not in old["vias"]]
    assert len(new_vias) == out["fanned_out"] == len(new_tracks)
    assert all(t.locked and t.net == NET for t in new_tracks)
    assert all(v.locked and v.net == NET for v in new_vias)
    pads = {(q.x, q.y): q for q in todo}
    for t in new_tracks:
        q = pads[t.start]
        rep = analyse_bends([t.start, t.end], t.width, pad_ends=(True, False))
        assert not (rep.non_octilinear or rep.ninety or rep.acute or rep.off_angle), t
        d = math.dist(t.start, t.end)
        ux, uy = (t.end[0] - t.start[0]) / d, (t.end[1] - t.start[1]) / d
        inside = 0.0
        while inside < d and _point_pad((q.x + ux * inside, q.y + uy * inside), q) == 0:
            inside += 0.001
        assert d - inside <= 1.0 + 0.002, (t, d, inside)
    drc = await rig.session.call(
        lambda h: api.run_drc_task(h, timeout_s=60), timeout=api.drc_call_timeout(60)
    )
    mine = {t.kiid for t in new_tracks} | {v.kiid for v in new_vias}
    bad = [
        m
        for m in drc.markers
        if m.type != "unconnected_items" and m.severity != "exclusion" and mine & set(m.kiids)
    ]
    assert bad == []


async def test_no_via_in_pad(copper_rig: PlacedSynth) -> None:
    rig = copper_rig
    await ensure_fanout(rig, NET)  # zones first: without them every via dangles
    m = await _model(rig, "run-vip-2")
    vias = [v for v in m.vias if v.net == NET]
    assert vias
    for v in vias:
        for p in m.parts:
            for q in p.pads:
                assert _point_pad(v.at, q) >= v.diameter / 2 - 1e-6, (v, q)


async def test_fanout_unknown_net(copper_rig: PlacedSynth) -> None:
    out, err = await call_tool(copper_rig.turn("run-unk"), "pcb_fanout", net="GNDD")
    assert err and out["error"]["code"] == "not_found", out
    assert NET in out["error"]["details"]["closest"]["GNDD"]


async def test_fanout_requires_approval(copper_rig: PlacedSynth, tmp_path: Path) -> None:
    """A database without the placement checkpoint → refused before any write."""
    from tests.kicad.board.conftest import open_setup_db

    fresh = await open_setup_db(tmp_path / "fresh.db", copper_rig.session.project.id)
    try:
        ctx = copper_rig.rig.turn(fresh, "run-na")
        out, err = await call_tool(ctx, "pcb_fanout", net=NET)
        assert err and out["error"]["code"] == "placement_not_approved", out
    finally:
        await fresh.close()


async def test_fanout_cost_2(copper_rig: PlacedSynth) -> None:
    await _ready(copper_rig, "run-cost")
    cs = await fanout.prepare_fanout(copper_rig.turn("run-cost-go"), {"net": NET})
    assert cs.logical.changes == 2


async def test_fanout_candidates_via_run_cpu(
    copper_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _ready(copper_rig, "run-cpu")
    calls: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []
    real = fanout.run_cpu

    async def spy(fn: Any, *args: Any, **kw: Any) -> Any:
        calls.append((fn, args, kw))
        return await real(fn, *args, **kw)

    monkeypatch.setattr(fanout, "run_cpu", spy)
    await fanout.prepare_fanout(copper_rig.turn("run-cpu-go"), {"net": NET})
    assert [c[0] for c in calls] == [fanout.fanout_candidates_job]
    model = calls[0][1][0]
    assert all(not z.filled and not z.filled_holes for z in model.zones)  # fill-stripped
    assert calls[0][2]["timeout_s"] == 30


async def test_m11_in_report(copper_rig: PlacedSynth) -> None:
    await _ready(copper_rig, "run-m11")
    out = await _apply(copper_rig, "run-m11-go")
    m11 = out["m11"]
    assert m11 is not None and m11["id"] == "M11" and m11["verdict"] in ("pass", "review", "fail")


# -- compensation ----------------------------------------------------------------------------


@pytest.mark.parametrize("n", [2, 4])
async def test_fanout_fail_after(copper_rig: PlacedSynth, n: int) -> None:
    """`fail_after=2` (after round 1's pushed refill) and `=4` (after round 2's add; stages
    are add, refill, check per round): KIID sets and board_hash equal pre-call."""
    rig = copper_rig
    await _ready(rig, f"run-fa{n}")
    ctx = rig.turn(f"run-fa{n}-go")
    before = await board_model(ctx)
    cs = await fanout.prepare_fanout(ctx, {"net": NET})
    cs.fail_after = n
    if ctx.on_mutation is not None:
        await ctx.on_mutation()
    with pytest.raises(RuntimeError, match="fail_after"):
        await fanout.apply_fanout(ctx, cs)
    after = await _model(rig, f"run-fa{n}-after")
    assert _ids(after) == _ids(before)
    assert board_hash(after) == board_hash(before)


async def test_fanout_refill_pushed_before_drc(
    copper_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every DRC runs outside an open commit and after its round's refill."""
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
    drcs = [i for i, e in enumerate(events) if e.startswith("drc")]
    assert drcs
    for i in drcs:
        assert events[i] == "drc@0"
        last_refill = max(j for j in range(i) if events[j].startswith("refill"))
        last_add = max(j for j in range(i) if events[j] == "commit")
        assert last_add < last_refill < i, events


# -- deadline and refill timeouts ------------------------------------------------------------


def _reject_one_via(monkeypatch: pytest.MonkeyPatch, clock: _Clock | None) -> list[str]:
    """Round 1's check also rejects one created via (a patched DRC marker); with `clock`
    the deadline passes right after it."""
    rejected: list[str] = []
    real = fanout.rejected_kiids

    def patched(drc: api.DrcResult, mine: set[str]) -> set[str]:
        out = real(drc, mine)
        if not rejected and mine:
            rejected.append(sorted(mine)[0])
            out |= {rejected[0]}
            if clock is not None:
                clock.offset += fanout.FANOUT_DEADLINE_S + 1
        return out

    monkeypatch.setattr(fanout, "rejected_kiids", patched)
    return rejected


def _spy_writes(monkeypatch: pytest.MonkeyPatch, session: Any) -> list[tuple[str, Any]]:
    """('refill', timeout_s) / ('drc', timeout_s) / ('call', session.call timeout of a
    refill or DRC) / ('commit', label) - every board write opens a commit."""
    log: list[tuple[str, Any]] = []
    real_refill, real_drc, real_call = api.refill, api.run_drc_task, session.call
    real_commit = session.commit

    def commit(label: str, **kw: Any) -> Any:
        log.append(("commit", label))
        return real_commit(label, **kw)

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
    monkeypatch.setattr(session, "commit", commit)
    return log


async def test_fanout_deadline(copper_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = copper_rig
    await _ready(rig, "run-dl")
    clock = _Clock()
    monkeypatch.setattr(fanout, "monotonic", clock)
    rejected = _reject_one_via(monkeypatch, clock)
    log = _spy_writes(monkeypatch, rig.session)
    out = await _apply(rig, "run-dl-go")
    monkeypatch.undo()
    assert rejected
    assert out["rounds"] == 1 and out["stopped_by_deadline"] is True
    assert out["fanned_out"] >= 1  # round 1's stubs kept
    assert any(u["reason"] == "timeout" for u in out["unconnected"])
    drc_at = min(i for i, e in enumerate(log) if e[0] == "drc")  # round 1's check
    after = [e for e in log[drc_at + 1 :] if e[0] in ("refill", "commit")]
    assert [e for e in after if e[0] == "refill"] == [("refill", api.REFILL_TIMEOUT_S)]
    assert after[-1] == ("refill", api.REFILL_TIMEOUT_S)  # the call's last API write
    await _ready(rig, "run-dl-clean")


@pytest.mark.parametrize("remove", [True, False])
async def test_fanout_final_refill_after_last_round(
    copper_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch, remove: bool
) -> None:
    """`MAX_ROUNDS` = 1: a removal in round 1's check → one final refill after it; no
    removal → none."""
    rig = copper_rig
    await _ready(rig, f"run-fin{remove}")
    monkeypatch.setattr(fanout, "MAX_ROUNDS", 1)
    if remove:
        _reject_one_via(monkeypatch, None)
    log = _spy_writes(monkeypatch, rig.session)
    await _apply(rig, f"run-fin{remove}-go")
    monkeypatch.undo()
    drc_at = min(i for i, e in enumerate(log) if e[0] == "drc")  # round 1's check
    refills = [e for e in log[drc_at + 1 :] if e[0] == "refill"]
    if remove:
        assert refills == [("refill", api.REFILL_TIMEOUT_S)]
    else:
        assert refills == []
    await _ready(rig, f"run-fin{remove}-clean")


@pytest.mark.parametrize("left", [20.0, 19.0])
async def test_fanout_deadline_bounds_calls(
    copper_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch, left: float
) -> None:
    """A frozen clock with `left` s remaining: 20 s → the refill gets timeout_s 20 (call
    timeout 65) and the DRC 20 s; 19 s → no refill in the round, its stubs and vias
    removed (pads `timeout`), then one cleanup refill with REFILL_TIMEOUT_S."""
    rig = copper_rig
    before = await _ready(rig, f"run-b{left:g}")
    clock = _Clock()
    clock.frozen = 1000.0
    monkeypatch.setattr(fanout, "monotonic", clock)
    ctx = rig.turn(f"run-b{left:g}-go")
    cs = await fanout.prepare_fanout(ctx, {"net": NET})
    clock.frozen = 1000.0 + fanout.FANOUT_DEADLINE_S - left
    log = _spy_writes(monkeypatch, rig.session)
    if ctx.on_mutation is not None:
        await ctx.on_mutation()
    result = await fanout.apply_fanout(ctx, cs)
    monkeypatch.undo()
    data = result.data
    assert isinstance(data, dict)
    refills = [e for e in log if e[0] == "refill"]
    if left == 20.0:
        assert refills[0] == ("refill", 20.0)
        i = log.index(refills[0])
        assert log[i + 1] == ("call", api.refill_call_timeout(20.0)) == ("call", 65.0)
        assert next(e for e in log if e[0] == "drc") == ("drc", 20.0)
    else:
        assert refills == [("refill", api.REFILL_TIMEOUT_S)]
        assert not [e for e in log if e[0] == "drc"]
        assert data["fanned_out"] == 0
        assert {u["reason"] for u in data["unconnected"]} <= {"timeout", "no_candidate"}
        assert any(u["reason"] == "timeout" for u in data["unconnected"])
        after = await _model(rig, f"run-b{left:g}-after")
        assert _ids(after)["vias"] == _ids(before)["vias"]
    await _ready(rig, f"run-b{left:g}-clean")


async def test_fanout_refill_timeout_in_round(
    copper_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`api.refill` raises RefillTimeout in round 1 → round 1's items removed, the cleanup
    refill runs, the call succeeds with the pads `timeout`."""
    rig = copper_rig
    before = await _ready(rig, "run-rt")
    real = api.refill
    calls: list[float] = []

    def refill(h: Any, *, timeout_s: float) -> None:
        calls.append(timeout_s)
        if len(calls) == 1:
            raise api.RefillTimeout(timeout_s)
        real(h, timeout_s=timeout_s)

    monkeypatch.setattr(api, "refill", refill)
    out = await _apply(rig, "run-rt-go")
    monkeypatch.undo()
    assert len(calls) == 2 and calls[1] == api.REFILL_TIMEOUT_S
    assert out["fanned_out"] == 0
    assert any(u["reason"] == "timeout" for u in out["unconnected"])
    after = await _model(rig, "run-rt-after")
    assert _ids(after)["vias"] == _ids(before)["vias"]
    await _ready(rig, "run-rt-clean")


async def test_zz_fanout_leaves_board_fanned(copper_rig: PlacedSynth) -> None:
    """Last in the module: the board is fanned out again for the later P7 modules."""
    rig = copper_rig
    await ensure_fanout(rig, NET)
    assert any(v.net == NET for v in (await _model(rig, "run-final-2")).vias)
