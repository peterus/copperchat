"""`pcb_route_fill` keeps earlier work and restores the full copper on failure (SP4 P6b
Task 3, index Review Focus 1): locked fanout survives repeated fills, the placement hash
holds, a failure after any stage, a stop and a rejected fill leave the board exactly as
it was, and no scratch dir stays behind.

One module server (`fill_rig`); tests run in file order and share it."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
from pathlib import Path
from typing import Any

import pytest

from copperchat.board import freerouting as fr
from copperchat.board.metrics import connections
from copperchat.board.model import BoardModel, placement_hash
from copperchat.board.router.chamfer import PostResult
from copperchat.kicad_tools.board import api, fill
from copperchat.kicad_tools.board.route import conn_key
from tests.kicad.board.conftest import PlacedSynth, setup_call
from tests.kicad.board.fill_support import (
    add_unlocked_track,
    fill_dirs,
    fill_ok,
    locked_gnd_fanout,
    model_of,
    remove_items,
    state,
    unroute,
)
from tests.kicad.board.fill_support import fill as run_fill

pytestmark = [pytest.mark.kicad, pytest.mark.asyncio(loop_scope="module")]

TIMEOUT = pytest.mark.timeout(480)


def _positions(m: BoardModel, ids: list[str]) -> dict[str, tuple[Any, ...]]:
    out: dict[str, tuple[Any, ...]] = {}
    for t in m.tracks:
        if t.kiid in ids:
            out[t.kiid] = (
                round(t.start[0], 3),
                round(t.start[1], 3),
                round(t.end[0], 3),
                round(t.end[1], 3),
                t.locked,
            )
    for v in m.vias:
        if v.kiid in ids:
            out[v.kiid] = (round(v.at[0], 3), round(v.at[1], 3), v.locked)
    return out


@TIMEOUT
async def test_refill_keeps_locked_vias(fill_rig: PlacedSynth, record_property: Any) -> None:
    ids, zone = await locked_gnd_fanout(fill_rig, "lf-0")
    want = _positions(await model_of(fill_rig, "lf-1"), ids)
    assert len(want) == len(ids) == 8
    for i in range(2):
        await unroute(fill_rig, f"lf-u{i}")
        out = await fill_ok(fill_rig, f"lf-f{i}")
        m = await model_of(fill_rig, f"lf-m{i}")
        assert _positions(m, ids) == want, i
        z = next((z for z in m.zones if z.kiid == zone), None)
        record_property(f"zone_same_kiid_{i}", z is not None)
        assert z is not None and z.filled_area_mm2 > 0, i
        # the refill ran on the final copper: no zone marker against the fill's tracks
        drc = await fill_rig.session.call(
            lambda h: api.run_drc_task(h, timeout_s=60), timeout=api.drc_call_timeout(60)
        )
        stale = [
            mk
            for mk in drc.markers
            if mk.severity == "error" and mk.type != "unconnected_items" and zone in mk.kiids
        ]
        assert stale == [], stale
        assert out["fills_cleared"] >= 1
        assert out["ground_items_deleted"] >= 0
    await remove_items(fill_rig, [*ids, zone])


@TIMEOUT
async def test_fill_keeps_placement_hash(fill_rig: PlacedSynth, record_property: Any) -> None:
    await unroute(fill_rig, "ph-0")
    before = placement_hash(await model_of(fill_rig, "ph-1"))
    out = await fill_ok(fill_rig, "ph-2")
    assert placement_hash(await model_of(fill_rig, "ph-3")) == before
    record_property("positions_restored", out["positions_restored"])
    assert out["positions_restored"] == 0


def _failing(monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    real = fill.apply_board_change_set

    async def failing(ctx: Any, cs: Any) -> Any:
        cs.fail_after = n
        return await real(ctx, cs)

    monkeypatch.setattr(fill, "apply_board_change_set", failing)


@TIMEOUT
@pytest.mark.parametrize("fail_after", [3, 4, 1])
async def test_fill_failure_restores_full_copper(
    fill_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch, fail_after: int
) -> None:
    """An unlocked non-ground track first (the SES import deletes unlocked copper), then a
    fill that fails after stage `fail_after`: copper (the unlocked track included), zones
    and `board_hash` as before; no scratch dir left."""
    await unroute(fill_rig, f"ff{fail_after}-0")
    track = await add_unlocked_track(fill_rig, f"ff{fail_after}-1", net="/SWDIO")
    copper, digest, m = await state(fill_rig, f"ff{fail_after}-2")
    zones = m.zones
    _failing(monkeypatch, fail_after)
    out, err = await run_fill(fill_rig, f"ff{fail_after}-3")
    assert err, out
    assert out["error"].get("code") != "document_inconsistent", out
    after_copper, after_digest, m2 = await state(fill_rig, f"ff{fail_after}-4")
    assert after_copper == copper
    assert m2.zones == zones
    assert after_digest == digest
    assert fill_dirs(fill_rig) == []
    await remove_items(fill_rig, [track])


@TIMEOUT
async def test_no_fill_dir_left(fill_rig: PlacedSynth) -> None:
    await unroute(fill_rig, "nd-0")
    await fill_ok(fill_rig, "nd-1")
    assert fill_dirs(fill_rig) == []


@TIMEOUT
async def test_fill_cancel_compensates(
    fill_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop while the JVM runs: CancelledError propagates, the JVM is gone (no /proc
    entry, no zombie), copper and `board_hash` as before, the session still works."""
    await unroute(fill_rig, "cc-0")
    copper, digest, _ = await state(fill_rig, "cc-1")
    procs: list[asyncio.subprocess.Process] = []
    real = fr.asyncio.create_subprocess_exec

    async def spy(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await real(*argv, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(fr.asyncio, "create_subprocess_exec", spy)
    task = asyncio.ensure_future(run_fill(fill_rig, "cc-2", timeout_s=300, passes=100))
    for _ in range(600):
        logs = [d / "freerouting.log" for d in fill_dirs(fill_rig)]
        if procs and any(p.exists() for p in logs):
            break
        await asyncio.sleep(0.05)
    else:
        task.cancel()
        raise AssertionError("Freerouting never started")
    await asyncio.sleep(0.5)
    pid = procs[-1].pid
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not Path(f"/proc/{pid}").exists()
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    after_copper, after_digest, _ = await state(fill_rig, "cc-3")
    assert after_copper == copper
    assert after_digest == digest
    assert fill_dirs(fill_rig) == []
    status = await setup_call(fill_rig.turn("cc-4"), "pcb_status")
    assert status["board_hash"] == digest


@TIMEOUT
async def test_rejected_fill_restores(
    fill_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-processor that rips every connection → `fill_not_acceptable` with reason
    `rip_ratio` and the worst areas; copper as before (Task 4's rule path)."""
    await unroute(fill_rig, "rj-0")
    copper, digest, _ = await state(fill_rig, "rj-1")
    real = fill._run_postprocess

    async def rip_all(model: BoardModel, kiids: frozenset[str], **kw: Any) -> PostResult:
        res = await real(model, kiids, **kw)
        nets = {q.net for p in model.parts for q in p.pads if q.net and q.net != "GND"}
        keys = tuple(conn_key(n, a, b) for n in sorted(nets) for a, b in connections(model, n))
        return dataclasses.replace(res, rip=keys)

    monkeypatch.setattr(fill, "_run_postprocess", rip_all)
    out, err = await run_fill(fill_rig, "rj-2")
    assert err and out["error"]["code"] == "fill_not_acceptable", out
    details = out["error"]["details"]
    assert details["reason"] == "rip_ratio"
    assert details["ratio"] > 0.2
    assert details["fill_connections"] >= 1
    assert 1 <= len(details["worst_areas"]) <= 5
    assert "pcb_render" in out["error"]["suggestion"]
    after_copper, after_digest, _ = await state(fill_rig, "rj-3")
    assert after_copper == copper
    assert after_digest == digest


@TIMEOUT
async def test_unlocked_ground_copper_survives(fill_rig: PlacedSynth) -> None:
    """Unlocked ground copper from before the call (e.g. hand-routed) keeps its KIID, its
    geometry and its unlocked state through a fill (locked for the round trip only)."""
    await unroute(fill_rig, "ug-0")
    kiid = await add_unlocked_track(fill_rig, "ug-1", net="GND")
    before = {t.kiid: t for t in (await model_of(fill_rig, "ug-2")).tracks}[kiid]
    out = await fill_ok(fill_rig, "ug-3")
    assert out["ground_protected"] >= 1
    after = {t.kiid: t for t in (await model_of(fill_rig, "ug-4")).tracks}.get(kiid)
    assert after == before
    await remove_items(fill_rig, [kiid])
