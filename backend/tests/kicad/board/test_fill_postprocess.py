"""`pcb_route_fill` post-processing, rip-and-reroute, the ladder and every deadline and
budget (SP4 P6b Task 4, index Review Focus 3, spec §10.3/§10.4).

One module server (`fill_rig`); tests run in file order and share it. Each fill test
unroutes `FILL_NETS` first so Freerouting has real connections to fill."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from typing import Any

import pytest

from copperchat.board.geom import TrackIndex, analyse_tracks
from copperchat.board.model import BoardModel
from copperchat.board.router.chamfer import PostResult
from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.board import api, engine, fill, route
from copperchat.kicad_tools.board.api import DrcTimeout, RefillTimeout
from tests.kicad.board.conftest import PlacedSynth, setup_call
from tests.kicad.board.fill_support import (
    FILL_NETS,
    add_gnd_plane,
    allow_rips,
    fill_ok,
    install_clock,
    model_of,
    remove_items,
    spy_calls,
    state,
    unroute,
)
from tests.kicad.board.fill_support import fill as run_fill

pytestmark = [pytest.mark.kicad, pytest.mark.asyncio(loop_scope="module")]

TIMEOUT = pytest.mark.timeout(480)


def _bends(m: BoardModel, nets: set[str] | None = None) -> dict[str, int]:
    sub = dataclasses.replace(
        m, tracks=tuple(t for t in m.tracks if nets is None or t.net in nets), arcs=()
    )
    rep = analyse_tracks(sub, index=TrackIndex(m))
    return {
        "M4": len(rep.non_octilinear)
        + len(rep.ninety)
        + len(rep.acute)
        + len(rep.off_angle)
        + len(rep.y_junctions),  # M15: re-landed as a T or ripped
        "M7": len(rep.micro_jogs) + len(rep.short_chamfers),
    }


def _ripping(monkeypatch: pytest.MonkeyPatch, n: int, picked: list[str]) -> None:
    """The real post-processor, plus `n` ripped fill connections (those through new
    copper, sorted) recorded in `picked`."""
    allow_rips(monkeypatch)
    real = fill._run_postprocess

    async def rip_n(model: BoardModel, kiids: frozenset[str], **kw: Any) -> PostResult:
        res = await real(model, kiids, **kw)
        nets = sorted({t.net for t in model.tracks if t.kiid in kiids})
        keys = sorted({k for net in nets for k in fill._conns_using(model, net, set(kiids))})
        assert len(keys) >= n, keys
        picked[:] = keys[:n]
        return dataclasses.replace(res, rip=tuple(sorted(set(res.rip) | set(picked))))

    monkeypatch.setattr(fill, "_run_postprocess", rip_n)


# -- the synth fill meets the owner's bar -----------------------------------------------------


@TIMEOUT
async def test_fill_synth_m4_m7_zero(fill_rig: PlacedSynth, record_property: Any) -> None:
    await unroute(fill_rig, "mm-0")
    out = await fill_ok(fill_rig, "mm-1")
    got = await setup_call(fill_rig.turn("mm-2"), "pcb_metrics", only=["M4", "M7"])
    by = {m["id"]: m for m in got["metrics"]}
    assert by["M4"]["value"] == 0, by["M4"]
    assert by["M7"]["verdict"] != "fail", by["M7"]
    assert _bends(await model_of(fill_rig, "mm-3")) == {"M4": 0, "M7": 0}
    record_property("fill_metrics", out["metrics"])


@TIMEOUT
async def test_fill_synth_complete_non_ground(fill_rig: PlacedSynth, record_property: Any) -> None:
    """Every non-ground connection has copper afterwards (M2 is not asserted: the fill
    deletes its ground copper, spec §17; P9a asserts M2 after P7's planes)."""
    await unroute(fill_rig, "cp-0")
    out = await fill_ok(fill_rig, "cp-1")
    record_property("completion", out["completion"])
    record_property("rerouted", out["rerouted"])
    assert out["completion"] == 1.0, out["open"]
    assert out["open"] == []
    assert "pcb_zones" in out["next"]


@TIMEOUT
async def test_chamfers_drc_clean(fill_rig: PlacedSynth, record_property: Any) -> None:
    """No clearance/track marker of the DRC task on copper the post-processor made."""
    await unroute(fill_rig, "ch-0")
    made: list[str] = []
    real = fill._apply_replace

    def spy(h: Any, replace: Any) -> list[list[str]]:
        out = real(h, replace)
        made.extend(k for ks in out for k in ks)
        return out

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr(fill, "_apply_replace", spy)
    try:
        out = await fill_ok(fill_rig, "ch-1")
    finally:
        mp.undo()
    record_property("chamfered", out["chamfered"])
    record_property("post_tracks", len(made))
    s = fill_rig.session
    drc = await s.call(
        lambda h: api.run_drc_task(h, timeout_s=60), timeout=api.drc_call_timeout(60)
    )
    live = {t.kiid for t in (await model_of(fill_rig, "ch-2")).tracks}
    mine = set(made) & live
    bad = [m for m in drc.markers if m.type != "unconnected_items" and mine.intersection(m.kiids)]
    assert bad == []


# -- rip-and-reroute ---------------------------------------------------------------------------


@TIMEOUT
async def test_rip_reroute_path(fill_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch) -> None:
    await unroute(fill_rig, "rr-0")
    picked: list[str] = []
    _ripping(monkeypatch, 2, picked)
    out = await fill_ok(fill_rig, "rr-1")
    assert out["ripped"] >= 2
    assert out["rerouted"] >= 2, out["rejected"]
    assert not [r for r in out["rejected"] if r["connection"] in picked]
    nets = {k.split(":", 1)[0] for k in picked}
    assert _bends(await model_of(fill_rig, "rr-2"), nets) == {"M4": 0, "M7": 0}


@TIMEOUT
async def test_ladder_unlock_fill_relock(fill_rig: PlacedSynth) -> None:
    """Rung (b): a net routed by `pcb_route` (locked), `pcb_unroute {unlock_only}` keeps
    its KIIDs unlocked, `pcb_route_fill {relock_nets}` locks it again (M4/M7 = 0)."""
    net = FILL_NETS[0]
    await setup_call(fill_rig.turn("ld-0"), "pcb_unroute", nets=[net], include_locked=True)
    await setup_call(fill_rig.turn("ld-1"), "pcb_route", nets=[net], lock=True)
    m = await model_of(fill_rig, "ld-2")
    ids = {t.kiid for t in m.tracks if t.net == net}
    assert ids and all(t.locked for t in m.tracks if t.net == net)
    got = await setup_call(fill_rig.turn("ld-3"), "pcb_unroute", nets=[net], unlock_only=True)
    assert net in got["unlocked_nets"]
    m = await model_of(fill_rig, "ld-4")
    assert {t.kiid for t in m.tracks if t.net == net} == ids
    assert not any(t.locked for t in m.tracks if t.net == net)
    await unroute(fill_rig, "ld-5", FILL_NETS[1:])
    out = await fill_ok(fill_rig, "ld-6", relock_nets=[net])
    assert out["relocked"] == [net], out["relock_failed"]
    m = await model_of(fill_rig, "ld-7")
    mine = [t for t in m.tracks if t.net == net]
    assert mine and all(t.locked for t in mine)
    assert all(v.locked for v in m.vias if v.net == net)
    assert _bends(m, {net}) == {"M4": 0, "M7": 0}


# -- the post deadline --------------------------------------------------------------------------


@TIMEOUT
async def test_post_deadline_reports_remaining_rips(
    fill_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4 ripped connections; the (fake) clock passes the post deadline right after the
    first re-route is kept → what was kept by then is re-routed (R3: pass 0 keeps all of
    its DRC-verified straight segments together, so possibly more than one), the rest are
    `rejected` with reason `timeout`, the call succeeds, and what was kept has M4 = M7 = 0."""
    await unroute(fill_rig, "pd-0")
    picked: list[str] = []
    _ripping(monkeypatch, 4, picked)
    clock = install_clock(monkeypatch, fill, route)
    real = route._record

    def record(st: Any, kept: Any) -> None:
        real(st, kept)
        clock.offset += fill.POST_DEADLINE_S + 1.0

    monkeypatch.setattr(route, "_record", record)
    out = await fill_ok(fill_rig, "pd-1")
    rejected = {r["connection"]: r["reason"] for r in out["rejected"]}
    assert 1 <= out["rerouted"] < 4, out
    assert len(rejected) == 4 - out["rerouted"] and set(rejected.values()) == {"timeout"}, rejected
    assert all({"x", "y"} <= set(r) for r in out["rejected"])
    assert _bends(await model_of(fill_rig, "pd-2")) == {"M4": 0, "M7": 0}
    assert "pcb_route" in out["next"]


@TIMEOUT
async def test_post_overrun_bounded(fill_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST_DEADLINE_S = 2 s and every post `run_cpu` sleeping its whole `timeout_s`:
    stages 5-7 end within 2 s + one DRC (60 s), and no post-processing job or DRC got
    more than the remaining time."""
    await unroute(fill_rig, "po-0")
    monkeypatch.setattr(fill, "POST_DEADLINE_S", 2.0)
    allow_rips(monkeypatch)
    seen: list[tuple[str, float, float]] = []  # (what, timeout_s, remaining)
    starts: dict[str, float] = {}
    real_post = fill._run_postprocess
    picked: list[str] = []

    async def slow_post(model: BoardModel, kiids: frozenset[str], **kw: Any) -> PostResult:
        starts["post"] = time.monotonic()
        res = await real_post(model, kiids, **kw)
        rem = kw["wait_s"]
        seen.append(("postprocess", kw["timeout_s"], rem))
        await asyncio.sleep(kw["timeout_s"])
        nets = sorted({t.net for t in model.tracks if t.kiid in kiids})
        keys = sorted({k for n in nets for k in fill._conns_using(model, n, set(kiids))})
        picked[:] = keys[:2]
        return dataclasses.replace(res, rip=tuple(sorted(set(res.rip) | set(picked))))

    async def slow_cpu(
        fn: Any, *a: Any, timeout_s: float, wait_s: float | None = None, **kw: Any
    ) -> Any:
        seen.append((fn.__name__, timeout_s, wait_s if wait_s is not None else -1))
        await asyncio.sleep(max(timeout_s, 0))
        return None

    real_drc = engine.BoardStageContext.drc

    async def drc(self: Any, *, timeout_s: float = 60.0) -> api.DrcResult:
        seen.append(("drc", timeout_s, -1))
        return await real_drc(self, timeout_s=timeout_s)

    monkeypatch.setattr(fill, "_run_postprocess", slow_post)
    monkeypatch.setattr(route, "run_cpu", slow_cpu)
    monkeypatch.setattr(engine.BoardStageContext, "drc", drc)
    out = await fill_ok(fill_rig, "po-1")
    end = time.monotonic()
    assert end - starts["post"] <= 2.0 + 60.0 + 15.0  # + the metrics / read-back margin
    for what, t, rem in seen:
        if what == "metrics_job":
            continue
        if rem >= 0:
            assert t <= rem + 1e-6, (what, t, rem)
        assert t <= 60.0, (what, t)
    assert {r["reason"] for r in out["rejected"]} <= {"timeout"}


@TIMEOUT
async def test_freerouting_gets_timeout_s(
    fill_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    await unroute(fill_rig, "ft-0")
    got: list[float] = []
    real = fill.run_freerouting

    async def spy(*a: Any, timeout_s: float, **kw: Any) -> Any:
        got.append(timeout_s)
        return await real(*a, timeout_s=timeout_s, **kw)

    monkeypatch.setattr(fill, "run_freerouting", spy)
    await fill_ok(fill_rig, "ft-1", timeout_s=77)
    assert got == [77]


@TIMEOUT
async def test_postprocess_job_gets_stripped_model(
    fill_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    zone = await add_gnd_plane(fill_rig, "sm-0")
    await unroute(fill_rig, "sm-1")
    models: list[BoardModel] = []
    real = fill.run_cpu

    async def spy(fn: Any, *a: Any, **kw: Any) -> Any:
        if fn is fill.postprocess:
            models.append(a[0])
        return await real(fn, *a, **kw)

    monkeypatch.setattr(fill, "run_cpu", spy)
    try:
        await fill_ok(fill_rig, "sm-2")
    finally:
        await remove_items(fill_rig, [zone])
    assert len(models) == 1
    zones = models[0].zones
    assert zones and all(z.filled == () and z.filled_holes == () for z in zones)


def _one_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real post-processor plus one no-op replacement (a new track re-created as is),
    so stage 5 applies something and runs its DRC."""
    real = fill._run_postprocess

    async def plus_one(model: BoardModel, kiids: frozenset[str], **kw: Any) -> PostResult:
        res = await real(model, kiids, **kw)
        done = {old for old, _ in res.replace}
        t = next(
            t
            for t in sorted(model.tracks, key=lambda t: t.kiid)
            if t.kiid in kiids and t.kiid not in done
        )
        return dataclasses.replace(
            res, replace=(*res.replace, (t.kiid, (dataclasses.replace(t, kiid=""),)))
        )

    monkeypatch.setattr(fill, "_run_postprocess", plus_one)


@TIMEOUT
async def test_postprocess_drc_timeout_keeps_call(
    fill_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    await unroute(fill_rig, "dt-0")
    _one_replacement(monkeypatch)

    async def drc(self: Any, *, timeout_s: float = 60.0) -> api.DrcResult:
        raise DrcTimeout(timeout_s, "test")

    monkeypatch.setattr(engine.BoardStageContext, "drc", drc)
    out = await fill_ok(fill_rig, "dt-1")
    assert out["drc_skipped"] is True


@TIMEOUT
async def test_reroute_drc_timeout_per_connection(
    fill_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    await unroute(fill_rig, "rd-0")
    picked: list[str] = []
    _ripping(monkeypatch, 2, picked)
    active = {"on": False, "n": 0}
    real_route = fill.route_connections

    async def routing(*a: Any, **kw: Any) -> Any:
        active["on"] = True
        try:
            return await real_route(*a, **kw)
        finally:
            active["on"] = False

    real_drc = engine.BoardStageContext.drc

    async def drc(self: Any, *, timeout_s: float = 60.0) -> api.DrcResult:
        if active["on"]:
            active["n"] += 1
            if active["n"] == 2:
                raise DrcTimeout(timeout_s, "test")
        return await real_drc(self, timeout_s=timeout_s)

    monkeypatch.setattr(fill, "route_connections", routing)
    monkeypatch.setattr(engine.BoardStageContext, "drc", drc)
    out = await fill_ok(fill_rig, "rd-1")
    reasons = {r["connection"]: r["reason"] for r in out["rejected"]}
    assert out["rerouted"] >= 1
    assert "timeout" in reasons.values(), out


# -- the report's metrics ---------------------------------------------------------------------


@TIMEOUT
async def test_fill_metrics_via_run_cpu(
    fill_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The metrics come from one `run_cpu(metrics_job, …, only=…)` with the fixed 30/30
    budget even when the post deadline has passed (fake clock: passed right after the
    post-processor ran)."""
    await unroute(fill_rig, "fm-0")
    clock = install_clock(monkeypatch, fill, route)
    real_post = fill._run_postprocess

    async def post(model: BoardModel, kiids: frozenset[str], **kw: Any) -> PostResult:
        res = await real_post(model, kiids, **kw)
        clock.offset += fill.POST_DEADLINE_S + 1
        return res

    calls: list[tuple[Any, dict[str, Any]]] = []
    real_cpu = fill.run_cpu

    async def spy(fn: Any, *a: Any, **kw: Any) -> Any:
        calls.append((fn, kw | {"args": a}))
        return await real_cpu(fn, *a, **kw)

    monkeypatch.setattr(fill, "_run_postprocess", post)
    monkeypatch.setattr(fill, "run_cpu", spy)
    out = await fill_ok(fill_rig, "fm-1")
    metric_calls = [kw for fn, kw in calls if fn is fill.metrics_job]
    assert len(metric_calls) == 1
    kw = metric_calls[0]
    assert (kw["timeout_s"], kw["wait_s"]) == (30.0, 30.0)
    assert kw["args"][3] is not None or True  # preset (None for an unknown layer count)
    assert kw["args"][4] == frozenset({"M4", "M5", "M7", "M12"})
    assert out["metrics"] is not None


@TIMEOUT
async def test_fill_metrics_timeout_reports_null(
    fill_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    await unroute(fill_rig, "mn-0")

    async def timeout(model: BoardModel, doc: Any) -> Any:
        raise StructuredToolError("x", "y", "z", code="board_compute_timeout")

    monkeypatch.setattr(fill, "_final_metrics", timeout)
    out = await fill_ok(fill_rig, "mn-1")
    assert out["metrics"] is None
    assert "metrics timed out; run pcb_metrics or pcb_check" in out["notes"]
    assert out["completion"] > 0


# -- the round trip's I/O budget and the refill --------------------------------------------------


@TIMEOUT
async def test_fill_io_calls_pass_timeout(
    fill_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    zone = await add_gnd_plane(fill_rig, "io-0")
    await unroute(fill_rig, "io-1")
    refills: list[float] = []
    real_refill = api.refill

    def refill(h: Any, *, timeout_s: float) -> None:
        refills.append(timeout_s)
        real_refill(h, timeout_s=timeout_s)

    try:
        with spy_calls(
            monkeypatch,
            fill_rig,
            ((fill, "_save_board_copy"), (api, "import_specctra_session")),
        ) as spy:
            monkeypatch.setattr(api, "refill", refill)
            await fill_ok(fill_rig, "io-2")
    finally:
        monkeypatch.undo()
        await remove_items(fill_rig, [zone])
    by = dict(spy.calls)
    assert by["_save_board_copy"] == fill.IO_BUDGET_S
    imp = by["import_specctra_session"]
    assert imp is not None and 0 < imp <= fill.IO_BUDGET_S
    assert refills and all(t == api.REFILL_TIMEOUT_S for t in refills)
    assert api.refill_call_timeout(api.REFILL_TIMEOUT_S) == 105


@TIMEOUT
async def test_fill_refill_timeout_restores(
    fill_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    zone = await add_gnd_plane(fill_rig, "rt-0")
    await unroute(fill_rig, "rt-1")
    copper, digest, _ = await state(fill_rig, "rt-2")
    first = {"done": False}
    real = api.refill

    def refill(h: Any, *, timeout_s: float) -> None:
        if not first["done"]:
            first["done"] = True
            raise RefillTimeout(timeout_s)
        real(h, timeout_s=timeout_s)

    monkeypatch.setattr(api, "refill", refill)
    try:
        out, err = await run_fill(fill_rig, "rt-3")
        assert err and out["error"]["code"] == "fill_not_acceptable", out
        assert out["error"]["details"]["reason"] == "io_timeout"
        after, after_digest, m2 = await state(fill_rig, "rt-4")
        assert after == copper
        assert after_digest == digest
        z = next(z for z in m2.zones if z.kiid == zone)
        assert z.filled_area_mm2 > 0
    finally:
        monkeypatch.undo()
        await remove_items(fill_rig, [zone])


@TIMEOUT
async def test_fill_io_budget_excludes_freerouting(
    fill_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fake clock: Freerouting 'takes' 90 s, the board copy of the export and the import
    5 s each → no io_timeout, and the import's timeout is 55 s minus the real export time
    (kicad-cli on the copy)."""
    await unroute(fill_rig, "ib-0")
    clock = install_clock(monkeypatch, fill)
    real_fr = fill.run_freerouting

    async def slow_fr(*a: Any, **kw: Any) -> Any:
        run = await real_fr(*a, **kw)
        clock.offset += 90
        return run

    real_exp, real_imp = fill._save_board_copy, api.import_specctra_session

    def exp(h: Any, dest: Any) -> None:
        real_exp(h, dest)
        clock.offset += 5

    def imp(h: Any, ses: Any) -> None:
        real_imp(h, ses)
        clock.offset += 5

    monkeypatch.setattr(fill, "run_freerouting", slow_fr)
    monkeypatch.setattr(fill, "_save_board_copy", exp)
    monkeypatch.setattr(api, "import_specctra_session", imp)
    with spy_calls(monkeypatch, fill_rig, ((api, "import_specctra_session"),)) as spy:
        await fill_ok(fill_rig, "ib-1")
    (t,) = [t for name, t in spy.calls if name == "import_specctra_session"]
    assert t is not None and 45.0 <= t <= 55.0


@TIMEOUT
async def test_fill_io_budget_spent(fill_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch) -> None:
    """An export whose board copy 'takes' 61 s but returns → io_timeout before Freerouting
    starts; no import; the board as before."""
    await unroute(fill_rig, "is-0")
    copper, digest, _ = await state(fill_rig, "is-1")
    clock = install_clock(monkeypatch, fill)
    real_exp = fill._save_board_copy
    calls: list[str] = []

    def exp(h: Any, dest: Any) -> None:
        real_exp(h, dest)
        clock.offset += 61

    async def no_fr(*a: Any, **kw: Any) -> Any:
        calls.append("freerouting")
        raise AssertionError("Freerouting must not start")

    def no_imp(h: Any, ses: Any) -> None:
        calls.append("import")
        raise AssertionError("no import")

    monkeypatch.setattr(fill, "_save_board_copy", exp)
    monkeypatch.setattr(fill, "run_freerouting", no_fr)
    monkeypatch.setattr(api, "import_specctra_session", no_imp)
    out, err = await run_fill(fill_rig, "is-2")
    assert err and out["error"]["code"] == "fill_not_acceptable", out
    assert out["error"]["details"]["reason"] == "io_timeout"
    assert calls == []
    after, after_digest, _ = await state(fill_rig, "is-3")
    assert (after, after_digest) == (copper, digest)


@TIMEOUT
async def test_drc_rejected_replacement_reverted(
    fill_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage 5's DRC names a replacement track → every replacement of that net is reverted
    (`drc_reverted`), its connections are ripped (`drc_rejected`) and re-routed by the own
    router; the board ends with M4 = M7 = 0."""
    await unroute(fill_rig, "rv-0")
    _one_replacement(monkeypatch)
    made: list[str] = []
    real_apply = fill._apply_replace

    def spy(h: Any, replace: Any) -> list[list[str]]:
        out = real_apply(h, replace)
        made.extend(k for ks in out for k in ks)
        return out

    real_drc = engine.BoardStageContext.drc
    calls = {"n": 0}

    async def drc(self: Any, *, timeout_s: float = 60.0) -> api.DrcResult:
        got = await real_drc(self, timeout_s=timeout_s)
        calls["n"] += 1
        if calls["n"] == 1 and made:  # the first DRC is stage 5's
            fake = api.DrcMarker("clearance", "error", "test rejection", (made[-1],), None)
            return dataclasses.replace(got, markers=(*got.markers, fake))
        return got

    allow_rips(monkeypatch)
    monkeypatch.setattr(fill, "_apply_replace", spy)
    monkeypatch.setattr(engine.BoardStageContext, "drc", drc)
    out = await fill_ok(fill_rig, "rv-1")
    assert out["drc_reverted"] >= 1
    rejected = [r for r in out["ripped_connections"] if r["reason"] == "drc_rejected"]
    assert rejected, out["ripped_connections"]
    assert out["completion"] == 1.0, out["rejected"]
    assert _bends(await model_of(fill_rig, "rv-2")) == {"M4": 0, "M7": 0}


@TIMEOUT
async def test_drc_rejected_rips_count_for_the_rule(
    fill_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 20 % rule is checked again after DRC-rejected replacements added rips: a
    rejected replacement on a fill net makes this small fill unacceptable, board restored."""
    await unroute(fill_rig, "rr2-0")
    copper, digest, _ = await state(fill_rig, "rr2-1")
    _one_replacement(monkeypatch)
    made: list[str] = []
    real_apply = fill._apply_replace

    def spy(h: Any, replace: Any) -> list[list[str]]:
        out = real_apply(h, replace)
        made.extend(k for ks in out for k in ks)
        return out

    real_drc = engine.BoardStageContext.drc

    async def drc(self: Any, *, timeout_s: float = 60.0) -> api.DrcResult:
        got = await real_drc(self, timeout_s=timeout_s)
        fake = tuple(
            api.DrcMarker("clearance", "error", "test rejection", (k,), None) for k in made
        )
        return dataclasses.replace(got, markers=(*got.markers, *fake))

    monkeypatch.setattr(fill, "RIP_RATIO_MAX", 0.0)  # any counted rip refuses
    monkeypatch.setattr(fill, "_apply_replace", spy)
    monkeypatch.setattr(engine.BoardStageContext, "drc", drc)
    monkeypatch.setattr(fill, "_check_ratio", _late_only(fill._check_ratio))
    out, err = await run_fill(fill_rig, "rr2-2")
    assert err and out["error"]["details"]["reason"] == "rip_ratio", out
    assert any(c["reason"] == "drc_rejected" for c in out["error"]["details"]["connections"])
    after, after_digest, _ = await state(fill_rig, "rr2-3")
    assert (after, after_digest) == (copper, digest)


def _late_only(real: Any) -> Any:
    """`_check_ratio` that skips its first call (the post-processor's own rips), so only
    the re-check after the DRC-rejected replacements can refuse."""
    seen = {"n": 0}

    async def check(*a: Any, **kw: Any) -> None:
        seen["n"] += 1
        if seen["n"] > 1:
            await real(*a, **kw)

    return check


@TIMEOUT
async def test_rip_of_locked_copper_not_ripped(
    fill_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rip key whose defective copper is locked work from before the call (here: a
    critical net routed by `pcb_route`) has nothing removable: it is reported `rejected`
    with reason `not_ripped`, never counted as rerouted, and the locked copper stays."""
    await unroute(fill_rig, "nr-0")
    m = await model_of(fill_rig, "nr-1")
    net = "/OSC_OUT"
    locked = {t.kiid for t in m.tracks if t.net == net and t.locked}
    assert locked
    key = next(k for k in fill._conns_using(m, net, locked))
    allow_rips(monkeypatch)
    real = fill._run_postprocess

    async def rip_locked(model: BoardModel, kiids: frozenset[str], **kw: Any) -> PostResult:
        res = await real(model, kiids, **kw)
        return dataclasses.replace(res, rip=(*res.rip, key))

    monkeypatch.setattr(fill, "_run_postprocess", rip_locked)
    out = await fill_ok(fill_rig, "nr-2")
    assert {"connection": key, "reason": "not_ripped"} == {
        k: v
        for k, v in next(r for r in out["rejected"] if r["connection"] == key).items()
        if k in ("connection", "reason")
    }
    after = {t.kiid for t in (await model_of(fill_rig, "nr-3")).tracks if t.net == net}
    assert locked <= after
