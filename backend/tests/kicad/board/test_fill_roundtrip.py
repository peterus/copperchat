"""`pcb_route_fill` round trip on the real api-server + Freerouting (SP4 P6b Tasks 1-2):
refusals, the probes that decided the implementation (SES import side effects, clearable
zone fills, the plane-layer mechanism), the DSN, and one real fill of `sp4_synth` with
the report fields.

One module server (`fill_rig`: P5b's routed rig); tests run in file order and share it.
Freerouting comes from `freerouting_rt` (fails in CI when missing, skips locally)."""

from __future__ import annotations

import dataclasses
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from copperchat.board.freerouting import FreeroutingRuntime, run_freerouting
from copperchat.board.model import BoardModel, board_hash, placement_hash
from copperchat.kicad_tools.board import api
from copperchat.kicad_tools.board.fill import disable_layers
from tests.kicad.board.conftest import PlacedSynth, open_setup_db, setup_call
from tests.kicad.board.fill_support import (
    FILL_NETS,
    add_gnd_plane,
    add_unlocked_track,
    export_live_dsn,
    fill,
    fill_dirs,
    fill_ok,
    model_of,
    remove_items,
    unroute,
)
from tests.support.edit import call_tool

pytestmark = [pytest.mark.kicad, pytest.mark.asyncio(loop_scope="module")]

TIMEOUT = pytest.mark.timeout(480)


def _ids(m: BoardModel) -> set[str]:
    return {t.kiid for t in m.tracks} | {a.kiid for a in m.arcs} | {v.kiid for v in m.vias}


# -- refusals (all in prepare: nothing written, nothing charged) -----------------------------


async def test_runtime_missing_refused(fill_rig: PlacedSynth, tmp_path: Path) -> None:
    before = board_hash(await model_of(fill_rig, "rt-0"))
    ctx = fill_rig.turn("rt-1")
    settings = ctx.settings.model_copy(update={"freerouting_home": tmp_path})
    ctx = dataclasses.replace(ctx, settings=settings)
    out, err = await call_tool(ctx, "pcb_route_fill")
    assert err and out["error"]["code"] == "freerouting_unavailable", out
    assert "COPPERCHAT_FREEROUTING_HOME" in out["error"]["suggestion"]
    assert ctx.editing is not None
    assert ctx.editing.turn(ctx.project.id, ctx.run_id).logical_used == 0
    assert board_hash(await model_of(fill_rig, "rt-2")) == before


async def test_no_placement_approval_refused(fill_rig: PlacedSynth, tmp_path: Path) -> None:
    fresh = await open_setup_db(tmp_path / "fresh.db", fill_rig.session.project.id)
    try:
        out, err = await call_tool(fill_rig.rig.turn(fresh, "na-1"), "pcb_route_fill")
        assert err and out["error"]["code"] == "placement_not_approved", out
        assert out["error"]["details"]["stale"] is False
    finally:
        await fresh.close()


async def test_stale_approval_refused(fill_rig: PlacedSynth) -> None:
    """A 0.5 mm move of a part without copper (a mounting hole) makes the approval stale;
    moved back exactly, it holds again."""
    m = await model_of(fill_rig, "st-0")
    hole = next(p for p in m.parts if p.ref.startswith("H"))
    await setup_call(
        fill_rig.turn("st-1"), "pcb_move", items=[{"ref": hole.ref, "x": hole.x + 0.5}]
    )
    out, err = await call_tool(fill_rig.turn("st-2"), "pcb_route_fill")
    assert err and out["error"]["code"] == "placement_not_approved", out
    assert out["error"]["details"]["stale"] is True
    await setup_call(fill_rig.turn("st-3"), "pcb_move", items=[{"ref": hole.ref, "x": hole.x}])
    m2 = await model_of(fill_rig, "st-4")
    assert placement_hash(m2) == fill_rig.info["placement_hash"]


async def test_unknown_relock_net(fill_rig: PlacedSynth) -> None:
    out, err = await call_tool(fill_rig.turn("unk"), "pcb_route_fill", relock_nets=["/SWDI0"])
    assert err and out["error"]["code"] == "not_found", out
    assert "/SWDIO" in out["error"]["details"]["closest"]["/SWDI0"]


async def test_unknown_plane_layer(fill_rig: PlacedSynth) -> None:
    out, err = await call_tool(fill_rig.turn("unk-l"), "pcb_route_fill", plane_layers=["In7.Cu"])
    assert err and out["error"]["code"] == "not_found", out
    out, err = await call_tool(fill_rig.turn("unk-l2"), "pcb_route_fill", plane_layers=["F.Cu"])
    assert err and out["error"]["code"] == "invalid_argument", out


async def test_plane_net_never_relocked(fill_rig: PlacedSynth) -> None:
    out, err = await call_tool(fill_rig.turn("gnd"), "pcb_route_fill", relock_nets=["GND"])
    assert err and out["error"]["code"] == "invalid_argument", out


# -- probes (recorded; they decided stage 1 and the plane-layer mechanism) -------------------


@pytest_asyncio.fixture(loop_scope="module")
async def dsn(fill_rig: PlacedSynth, tmp_path: Path) -> Path:
    return await export_live_dsn(fill_rig, tmp_path / "dsn")


async def test_probe_api_dsn_job_stale(
    fill_rig: PlacedSynth, tmp_path: Path, record_property: Any
) -> None:
    """Why the fill exports through kicad-cli on a board copy: the API job
    `RunBoardJobExportSpecctraDsn` keeps the board of its first run - after a track is
    added, a second export (also after a save) still lacks it."""

    def wires(p: Path) -> int:
        return p.read_text(encoding="utf-8").count("(wire")

    s = fill_rig.session
    first = tmp_path / "1.dsn"
    await s.call(lambda h: api.export_specctra_dsn(h, first), timeout=60)
    track = await add_unlocked_track(fill_rig, "job-0")
    await s.save("pcb")
    second = tmp_path / "2.dsn"
    await s.call(lambda h: api.export_specctra_dsn(h, second), timeout=60)
    live = await export_live_dsn(fill_rig, tmp_path / "cli")
    stale = wires(second) == wires(first)
    record_property("api_dsn_job_stale", stale)
    assert wires(live) == wires(first) + 1
    await remove_items(fill_rig, [track])


@TIMEOUT
async def test_probe_ses_import_side_effects(
    fill_rig: PlacedSynth,
    freerouting_rt: FreeroutingRuntime,
    tmp_path: Path,
    record_property: Any,
) -> None:
    """Export → Freerouting → import with one unlocked non-ground track and the locked
    critical routing on the board, the import inside a board commit that is then
    dropped: the locked copper keeps its KIIDs; the unlocked track's fate, footprint moves
    and whether the dropped commit took the import with it are recorded."""
    unlocked = await add_unlocked_track(fill_rig, "pr-0")
    before = await model_of(fill_rig, "pr-1")
    locked = {t.kiid for t in before.tracks if t.locked}
    assert locked
    ses = tmp_path / "b.ses"
    s = fill_rig.session
    dsn = await export_live_dsn(fill_rig, tmp_path / "x")
    run = await run_freerouting(
        freerouting_rt, dsn, ses, timeout_s=120, passes=10, workdir=tmp_path
    )
    assert run.returncode == 0 and ses.is_file(), run.log_tail
    async with s.commit("test: probe import", doc="pcb"):
        await s.call(lambda h: api.import_specctra_session(h, ses), timeout=60)
    after = await model_of(fill_rig, "pr-2")
    new = _ids(after) - _ids(before)
    same = [
        t
        for t in after.tracks
        if t.kiid in new and t.layer == "B.Cu" and t.net == "/SWDIO" and t.width == 0.2
    ]
    record_property("ses_unlocked_deleted", unlocked not in _ids(after))
    record_property("ses_unlocked_recreated", bool(same))
    record_property("ses_footprints_moved", placement_hash(after) != placement_hash(before))
    record_property("ses_new_items", len(new))
    assert locked <= {t.kiid for t in after.tracks if t.locked}
    # dropped commit: the import is staged by the commit like any other write
    n0 = len(after.tracks)
    async with s.commit("test: probe import (dropped)", doc="pcb", dry_run=True):
        await s.call(lambda h: api.import_specctra_session(h, ses), timeout=60)
    record_property(
        "ses_import_staged_by_commit", len((await model_of(fill_rig, "pr-3")).tracks) == n0
    )
    # back to the rig: remove what the import added, then the test track if it survived
    await remove_items(fill_rig, sorted(new | ({unlocked} & _ids(after))))
    assert _ids(await model_of(fill_rig, "pr-4")) == _ids(before) - {unlocked}


# R4 Task 2: candidate settings (pass / optimizer / cost options of the 2.5.0 JAR's
# settings classes; thread counts and the pass count are locked, never probed here)
PROBE_SETTINGS = (
    "--router.optimizer.enabled=true",
    "--router.optimizer.max_passes=5",
    "--router.optimizer.improvement_threshold=0.01",
    "--router.optimizer.item_selection_strategy=prioritized",
    "--router.optimizer.max_consecutive_failures=3",
    "--router.scoring.start_ripup_costs=100",
    "--router.scoring.plane_via_costs=5",
    "--router.scoring.default_bend_cost=1",
    "--router.automatic_neckdown=false",
    "--router.trace_pull_tight_accuracy=500",
    "--router.via_costs=50",  # not a 2.5.0 name (control)
    "--router.autorouter.via_costs=50",  # not a 2.5.0 name (control)
)


@TIMEOUT
async def test_probe_freerouting_settings(
    fill_rig: PlacedSynth,
    freerouting_rt: FreeroutingRuntime,
    tmp_path: Path,
    record_property: Any,
) -> None:
    """Which candidate settings the pinned Freerouting accepts: one run with all of them;
    every name it does not know is logged as "Unknown settings property: <name>". Records
    the accepted / unknown names and the optimizer's log lines (R4: whether the optimizer
    runs at all - P6b saw it skipped because locked links read as unrouted)."""
    dsn = await export_live_dsn(fill_rig, tmp_path / "x")
    run = await run_freerouting(
        freerouting_rt,
        dsn,
        tmp_path / "b.ses",
        timeout_s=120,
        passes=3,
        workdir=tmp_path / "w",
        extra_args=PROBE_SETTINGS,
    )
    assert run.returncode == 0, run.log_tail
    log = (tmp_path / "w" / "freerouting.log").read_text(errors="replace")
    unknown = set(re.findall(r"Unknown settings property: (\S+)", log))
    names = {a.split("=", 1)[0].removeprefix("--") for a in PROBE_SETTINGS}
    record_property("fr_settings_unknown", sorted(unknown))
    record_property("fr_settings_accepted", sorted(names - unknown))
    record_property(
        "fr_optimizer_log", [ln[-160:] for ln in log.splitlines() if "ptimiz" in ln][:10]
    )
    assert {"router.via_costs", "router.autorouter.via_costs"} <= unknown
    assert "router.optimizer.enabled" not in unknown


async def test_probe_zone_fill_clearable(fill_rig: PlacedSynth, record_property: Any) -> None:
    from copperchat.kicad_tools.board.fill import _clear_fills

    kiid = await add_gnd_plane(fill_rig, "zf-0")
    m = await model_of(fill_rig, "zf-1")
    assert next(z for z in m.zones if z.kiid == kiid).filled_area_mm2 > 0
    s = fill_rig.session
    async with s.commit("test: clear", doc="pcb"):
        await s.call(lambda h: _clear_fills(h, [kiid]))
    m2 = await model_of(fill_rig, "zf-2")
    cleared = next(z for z in m2.zones if z.kiid == kiid).filled_area_mm2 == 0
    record_property("zone_fill_clearable", cleared)
    assert cleared
    await remove_items(fill_rig, [kiid])


def _wires_on(ses: str, layer: str) -> int:
    return len(re.findall(r"\(path\s+" + re.escape(layer) + r"\s", ses))


@TIMEOUT
async def test_probe_plane_layer_mechanism(
    fill_rig: PlacedSynth,
    freerouting_rt: FreeroutingRuntime,
    dsn: Path,
    tmp_path: Path,
    record_property: Any,
) -> None:
    """Which mechanism keeps In1.Cu free: (a) a Freerouting CLI option (none exists for a
    layer in 2.5.0: indexed `--router.layers…` paths are rejected), (b) the plan's
    `(type power)` rewrite (no effect in 2.5.0), (b') the inserted autoroute layer rule
    (`disable_layers`, what the tool uses)."""
    text = dsn.read_text(encoding="utf-8")
    variants: dict[str, tuple[str, tuple[str, ...]]] = {
        "plain": (text, ()),
        "cli_layers_index": (text, ("--router.layers.1.routable=false",)),
        "type_power": (
            re.sub(r"(\(layer In1\.Cu\s*\(type )signal\)", r"\1power)", text, count=1),
            (),
        ),
        "autoroute_settings": (disable_layers(text, ["In1.Cu"]), ()),
    }
    on_in1: dict[str, int] = {}
    for name, (body, extra) in variants.items():
        wd = tmp_path / name
        wd.mkdir()
        src, ses = wd / "b.dsn", wd / "b.ses"
        src.write_text(body, encoding="utf-8")
        run = await run_freerouting(
            freerouting_rt, src, ses, timeout_s=120, passes=10, workdir=wd, extra_args=extra
        )
        assert run.returncode == 0 and ses.is_file(), (name, run.log_tail)
        on_in1[name] = _wires_on(ses.read_text(encoding="utf-8"), "In1.Cu")
        record_property(f"in1_wires_{name}", on_in1[name])
        if name == "cli_layers_index":
            log = (wd / "freerouting.log").read_text(errors="replace")
            record_property("cli_layers_index_rejected", "Unknown settings property" in log)
        shutil.rmtree(wd)
    record_property("plane_layer_mechanism", "dsn_autoroute_settings")
    assert on_in1["autoroute_settings"] == 0


async def test_dsn_layer_rewrite(dsn: Path) -> None:
    """On a DSN exported live from the rig (never stored): the rewrite adds exactly one
    line, the autoroute layer rule of the plane layer, and changes nothing else."""
    text = dsn.read_text(encoding="utf-8")
    out = disable_layers(text, ["In1.Cu"])
    old, new = text.splitlines(), out.splitlines()
    assert len(new) == len(old) + 1
    assert [ln for ln in new if ln not in old] == [
        next(ln for ln in new if "autoroute_settings" in ln)
    ]
    assert "(layer_rule In1.Cu (active off))" in out
    assert [ln for ln in new if "autoroute_settings" not in ln] == old


async def test_dsn_marks_locked_as_fixed(fill_rig: PlacedSynth, dsn: Path) -> None:
    """KiCad's exporter writes locked copper as `(type fix)` wiring (the plan expected
    `protect`; Freerouting keeps both): every locked track's net appears so."""
    m = await model_of(fill_rig, "dsn-0")
    nets = {t.net for t in m.tracks if t.locked}
    text = dsn.read_text(encoding="utf-8")
    fixed = {
        a or b for a, b in re.findall(r'\(net\s+(?:"([^"]*)"|([^\s)]+))\)\s*\(type fix\)', text)
    }
    assert nets and nets <= fixed, nets - fixed


# -- one real fill ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def filled(fill_rig: PlacedSynth) -> dict[str, Any]:
    """`FILL_NETS` unrouted (so the fill has real work), then one `pcb_route_fill`."""
    await unroute(fill_rig, "fl-0")
    before = await model_of(fill_rig, "fl-1")
    ctx = fill_rig.turn("fl-2")
    out = await fill_ok(fill_rig, "fl-2")
    after = await model_of(fill_rig, "fl-3")
    return {"before": before, "after": after, "out": out, "run": ctx.run_id}


@TIMEOUT
async def test_fill_routes_synth(filled: dict[str, Any], record_property: Any) -> None:
    out = filled["out"]
    assert out["open_before"] >= len(FILL_NETS)
    assert len(out["open"]) < out["open_before"]
    assert out["fill_routed"] >= 1
    record_property("fill_seconds", out["seconds"])
    record_property("freerouting_seconds", out["freerouting"]["seconds"])
    record_property("io_seconds", out["io_seconds"])
    record_property("completion", out["completion"])
    record_property("ground_items_deleted", out["ground_items_deleted"])
    record_property("redundant_items_deleted", out["redundant_items_deleted"])
    record_property("chamfered", out["chamfered"])
    record_property("ripped", out["ripped"])
    record_property("rerouted", out["rerouted"])
    assert out["freerouting"]["seconds"] > 0


async def test_no_fill_copper_on_plane_layers(filled: dict[str, Any]) -> None:
    old = _ids(filled["before"])
    assert not [t for t in filled["after"].tracks if t.kiid not in old and t.layer == "In1.Cu"]


async def test_fill_keeps_class_widths(filled: dict[str, Any]) -> None:
    """No fill track below its net's class width (M12): Freerouting necks traces down at
    pins; the post-processing stage widens them (`widened`)."""
    old = _ids(filled["before"])
    after: BoardModel = filled["after"]
    new = [t for t in after.tracks if t.kiid not in old]
    assert new
    assert min(t.width for t in new) >= 0.2 - 1e-6, sorted({(t.net, t.width) for t in new})


async def test_ground_copper_deleted(filled: dict[str, Any]) -> None:
    old = _ids(filled["before"])
    after: BoardModel = filled["after"]
    new_gnd = [
        i for i in (*after.tracks, *after.arcs, *after.vias) if i.net == "GND" and i.kiid not in old
    ]
    assert new_gnd == []
    assert filled["out"]["ground_items_deleted"] > 0


async def test_report_fields(filled: dict[str, Any]) -> None:
    out = filled["out"]
    for key in (
        "completion",
        "fill_routed",
        "ground_items_deleted",
        "positions_restored",
        "plane_layer_mechanism",
        "freerouting",
        "open",
        "next",
        "chamfered",
        "merged",
        "ripped",
        "rerouted",
        "rejected",
        "relocked",
        "relock_failed",
        "metrics",
    ):
        assert key in out, key
    assert set(out["freerouting"]) == {"seconds", "passes", "timed_out"}
    assert out["freerouting"]["passes"] == 30
    assert out["plane_layers"] == ["In1.Cu"]
    assert out["plane_layer_mechanism"] == "dsn_autoroute_settings"
    assert len(out["open"]) <= 50
    for o in out["open"]:
        assert {"connection", "a", "b", "x", "y"} <= set(o)
    assert isinstance(out["next"], str) and out["next"]
    assert out["positions_restored"] == 0
    assert placement_hash(filled["after"]) == placement_hash(filled["before"])


async def test_budget_cost_5(filled: dict[str, Any]) -> None:
    assert filled["out"]["logical_changes"] == 5
    assert filled["out"]["budget"]["used"] == 5


async def test_nothing_to_fill(fill_rig: PlacedSynth, filled: dict[str, Any]) -> None:
    """After a complete fill there is nothing left: refused, nothing written."""
    if filled["out"]["open"]:
        pytest.skip("the fill left open connections")
    out, err = await fill(fill_rig, "nf-1")
    assert err and out["error"]["code"] == "invalid_argument", out
    assert "nothing to fill" in out["error"]["why"]
    assert fill_dirs(fill_rig) == []
