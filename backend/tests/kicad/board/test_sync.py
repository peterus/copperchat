"""`pcb_sync_from_schematic` on the real api-server (SP4 P2 Task 1), on `sp4_synth`.

Every call goes through the real two-phase adapter (`call_tool`). Schematic changes are
made through the schematic API and saved by KiCad; no KiCad file is written by hand.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any

import pytest

from copperchat.board.geom import polygons_overlap
from copperchat.board.model import BoardModel, board_hash, placement_hash
from copperchat.history.service import HistoryService
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools import project as project_tools
from copperchat.kicad_tools.board import api, sync
from copperchat.kicad_tools.board.read import load_board_model
from copperchat.tools.spec import ToolContext
from tests.kicad.board.conftest import SynthRig
from tests.support.edit import call_tool

pytestmark = pytest.mark.kicad
MM = 1_000_000
SYNTH_PARTS = 26


async def _sync(ctx: ToolContext, **args: Any) -> tuple[dict[str, Any], bool]:
    if ctx.on_mutation is not None:
        await ctx.on_mutation()  # the turn snapshot (the run engine takes it once per turn)
    return await call_tool(ctx, "pcb_sync_from_schematic", **args)


async def _model(ctx: ToolContext) -> BoardModel:
    project = ctx.project
    return await ctx.session.call(lambda h: load_board_model(h, project), timeout=120)


def _hashes(project_dir: Path) -> dict[str, str]:
    out = {}
    for p in sorted(project_dir.iterdir()):
        if p.is_file() and p.suffix.startswith(".kicad"):
            out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


async def test_sync_create_then_undo(synth_rig: SynthRig) -> None:
    ctx = synth_rig.ctx()
    t0 = time.monotonic()
    data, err = await _sync(ctx)
    print("sync on sp4_synth (create):", round(time.monotonic() - t0, 2), "s")
    assert not err, data
    pcb = ctx.project.pro_file.with_suffix(".kicad_pcb")
    assert pcb.is_file()
    assert HistoryService.docs_of(ctx.project) == {"sch", "pcb"}
    assert data["footprints"] == SYNTH_PARTS
    assert len(data["added"]) == SYNTH_PARTS and data["removed"] == []
    assert data["board_created"] is True and data["next"] == "pcb_set_outline"

    await synth_rig.restore_turn(ctx)
    assert not pcb.exists()
    fresh = synth_rig.session
    assert not fresh.closed and fresh.project.pcb_file is None
    assert HistoryService.docs_of(fresh.project) == {"sch"}
    info = await project_tools.project_info(
        ToolContext(project=fresh.project, session=fresh, settings=fresh.settings), {}
    )
    assert info.data is not None and info.data["files"]["board"] is None
    errors = [text for text, _ in synth_rig.engine.errors]
    assert not any("could not open" in e for e in errors), errors


async def test_sync_all_staged(synth_rig: SynthRig) -> None:
    ctx = synth_rig.ctx()
    data, err = await _sync(ctx)
    assert not err, data
    m = await _model(ctx)
    assert data["unplaced"] == data["footprints"] == len(m.parts) == SYNTH_PARTS
    for p in m.parts:
        assert all(x >= 0 for x, _ in p.courtyard), p.ref
    with_cy = [p for p in m.parts if p.courtyard]
    for i, a in enumerate(with_cy):
        for b in with_cy[i + 1 :]:
            assert not polygons_overlap(list(a.courtyard), list(b.courtyard)), (a.ref, b.ref)
    drc = await ctx.session.call(lambda h: api.run_drc_task(h), timeout=api.drc_call_timeout(60))
    assert not [mk for mk in drc.markers if mk.type == "courtyards_overlap"]
    box = data["staging_box"]
    assert box["x"] >= 0 and box["y"] >= 0 and box["w"] > 0 and box["h"] > 0


async def test_schematic_unsaved(synth_rig: SynthRig) -> None:
    ctx = synth_rig.ctx()
    data, err = await _sync(ctx)
    assert not err, data
    ctx = synth_rig.ctx()
    before = await _model(ctx)

    def change_value(h: KicadHandle) -> None:
        sch = h.schematic()
        r4 = next(s for s in sch.get_symbols() if s.reference == "R4")
        r4.value = "22k"
        sch.update_items(r4)

    async with ctx.session.commit("test: unsaved value"):
        await ctx.session.call(change_value)
    data, err = await _sync(ctx)
    assert err and data["error"]["code"] == "schematic_unsaved", data
    after = await _model(ctx)
    assert board_hash(after) == board_hash(before)


async def test_dry_run_writes_nothing(synth_rig: SynthRig) -> None:
    ctx = synth_rig.ctx()
    data, err = await _sync(ctx)
    assert not err, data
    ctx = synth_rig.ctx()
    files = _hashes(ctx.project.pro_file.parent)
    m0 = await _model(ctx)
    budget_before = await _spent(ctx)
    data, err = await _sync(ctx, dry_run=True)
    assert not err, data
    assert data["dry_run"] is True and data["new_footprints"] == 0 and data["error_count"] == 0
    assert _hashes(ctx.project.pro_file.parent) == files
    m1 = await _model(ctx)
    assert (placement_hash(m1), board_hash(m1)) == (placement_hash(m0), board_hash(m0))
    assert await _spent(ctx) == budget_before


async def test_dry_run_without_board(synth_rig: SynthRig) -> None:
    ctx = synth_rig.ctx()
    data, err = await _sync(ctx, dry_run=True)
    assert not err, data
    assert data["creates_board"] is True and data["footprints"] == SYNTH_PARTS
    assert ctx.project.pcb_file is None
    assert not ctx.project.pro_file.with_suffix(".kicad_pcb").exists()


async def _spent(ctx: ToolContext) -> int:
    assert ctx.editing is not None
    info = await ctx.editing.gate.budget_info(ctx)
    return int(info["used"])


async def test_budget_cost_3(synth_rig: SynthRig) -> None:
    ctx = synth_rig.ctx()
    before = await _spent(ctx)
    data, err = await _sync(ctx)
    assert not err, data
    assert await _spent(ctx) - before == sync.SYNC_COST == 3


async def test_symlinked_pcb_refused(synth_rig: SynthRig, tmp_path: Path) -> None:
    ctx = synth_rig.ctx()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "victim.kicad_pcb"
    pcb = ctx.project.pro_file.with_suffix(".kicad_pcb")
    os.symlink(target, pcb)
    data, err = await _sync(ctx)
    assert err and data["error"]["code"] == "output_path_unsafe", data
    assert not target.exists() and list(outside.iterdir()) == []
    assert os.path.islink(pcb)
    assert HistoryService.docs_of(ctx.project) == {"sch"}


async def _synced(rig: SynthRig) -> ToolContext:
    data, err = await _sync(rig.ctx())
    assert not err, data
    return rig.ctx()


def _positions(m: BoardModel, refs: list[str]) -> dict[str, tuple[float, float]]:
    return {p.ref: (p.x, p.y) for p in m.parts if p.ref in refs}


async def test_resync_keeps_placement(synth_rig: SynthRig) -> None:
    from kipy.geometry import Vector2

    ctx = await _synced(synth_rig)

    def move(h: KicadHandle) -> None:
        board = h.board()
        fps = {f.reference_field.text.value: f for f in board.get_footprints()}
        fps["U1"].position = Vector2.from_xy(30 * MM, 30 * MM)
        fps["C1"].position = Vector2.from_xy(40 * MM, 32 * MM)
        board.update_items([fps["U1"], fps["C1"]])

    async with ctx.session.commit("test: place U1, C1", doc="pcb"):
        await ctx.session.call(move)
    await ctx.session.save("pcb")
    placed = _positions(await _model(ctx), ["U1", "C1"])
    data, err = await call_tool(ctx, "sch_set_fields", ref="R4", fields={"value": "22k"})
    assert not err, data
    ctx = synth_rig.ctx()
    data, err = await _sync(ctx)
    assert not err, data
    assert data["changed"] == ["R4"] and data["added"] == [] and data["removed"] == []
    m = await _model(ctx)
    for ref, (x, y) in _positions(m, ["U1", "C1"]).items():
        assert abs(x - placed[ref][0]) <= 0.001 and abs(y - placed[ref][1]) <= 0.001, ref
    assert m.part("R4").value == "22k"


async def test_delete_extra(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    data, err = await call_tool(ctx, "sch_delete", items=["R1"])
    assert not err, data
    ctx = synth_rig.ctx()
    data, err = await _sync(ctx, delete_extra=False)
    assert not err, data
    assert data["removed"] == [] and "R1" in {p.ref for p in (await _model(ctx)).parts}
    data, err = await _sync(ctx)
    assert not err, data
    assert data["removed"] == ["R1"]
    assert "R1" not in {p.ref for p in (await _model(ctx)).parts}


async def test_pad_warning_suggests_fp_check(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    data, err = await call_tool(
        ctx, "sch_set_fields", ref="U3", fields={"footprint": "Package_TO_SOT_SMD:SOT-23"}
    )
    assert not err, data
    ctx = synth_rig.ctx()
    data, err = await _sync(ctx)
    assert not err, data
    print("sync warnings:", data["warnings"])
    rows = [w for w in data["warnings"] if w.get("ref") == "U3"]
    assert rows, data["warnings"]
    assert rows[0]["suggestion"] == "check the footprint of U3 with fp_check"
    assert data["changed"] == ["U3"]


async def test_failure_after_clean_dry_run(
    synth_rig: SynthRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from copperchat.kicad_tools.board import compensate, engine

    ctx = await _synced(synth_rig)
    real_import = sync._import
    calls: list[bool] = []

    def failing_import(h: KicadHandle, netlist: Path, *, dry_run: bool, delete_extra: bool) -> Any:
        calls.append(dry_run)
        if not dry_run:
            raise RuntimeError("import exploded")
        return real_import(h, netlist, dry_run=dry_run, delete_extra=delete_extra)

    restored: list[Any] = []
    monkeypatch.setattr(sync, "_import", failing_import)
    monkeypatch.setattr(engine, "restore", lambda *a: restored.append(a) or {})
    monkeypatch.setattr(compensate, "restore", lambda *a: restored.append(a) or {})
    data, err = await _sync(ctx)
    assert err and data["error"]["code"] == "sync_failed", data
    assert "History panel" in data["error"]["suggestion"]
    assert calls == [True, False] and restored == []


async def test_failure_on_created_board_dry_run(
    synth_rig: SynthRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = synth_rig.ctx()

    def dirty_dry_run(h: KicadHandle, netlist: Path, *, dry_run: bool, delete_extra: bool) -> Any:
        assert dry_run, "the real import must not run"
        return {"errors": 1, "warnings": 0, "new": 0, "report": "Error: something is off\n"}

    monkeypatch.setattr(sync, "_import", dirty_dry_run)
    data, err = await _sync(ctx)
    assert err and data["error"]["code"] == "sync_failed", data
    assert "something is off" in data["error"]["why"]
    assert not ctx.project.pro_file.with_suffix(".kicad_pcb").exists()
    assert HistoryService.docs_of(synth_rig.session.project) == {"sch"}
