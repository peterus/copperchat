"""`pcb_export_3d` on the real api-server (SP4 P8 Task 4): STEP and GLB of the routed synth
(`fab_rig`) into `fab/3d/`, the shared safe path helper, the atomic replace, the edit lock and
the CLI lock. No approval needed."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import shutil
from pathlib import Path
from typing import Any

import pytest

from copperchat.kicad_tools.board import fab as fab_tool
from copperchat.kicad_tools.board.read import board_model
from copperchat.kicad_tools.cli import CliResult
from copperchat.views.board_render import BOARD_CLI_LOCK
from tests.kicad.board.conftest import PlacedSynth, setup_call
from tests.support.edit import call_tool

pytestmark = [
    pytest.mark.kicad,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.timeout(600),  # the first test of a shard builds the session rig
]


def _fab(rig: PlacedSynth) -> Path:
    return rig.session.project.dir / "fab"


def _out(rig: PlacedSynth, ext: str) -> Path:
    return _fab(rig) / "3d" / f"{rig.session.project.pro_file.stem}.{ext}"


def _clean(rig: PlacedSynth) -> None:
    fab = _fab(rig)
    if fab.is_symlink() or fab.is_file():
        fab.unlink()
    elif fab.exists():
        shutil.rmtree(fab)


def _leftovers(rig: PlacedSynth) -> list[str]:
    return [p.name for p in _fab(rig).iterdir() if ".tmp-" in p.name or ".old-" in p.name]


async def _export(rig: PlacedSynth, fmt: str, run: str) -> tuple[dict[str, Any], bool]:
    return await call_tool(rig.turn(run), "pcb_export_3d", format=fmt)


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


async def test_step(fab_rig: PlacedSynth, record_property: Any) -> None:
    _clean(fab_rig)
    out, err = await _export(fab_rig, "step", "run-3d-step")
    assert not err, out
    path = _out(fab_rig, "step")
    assert path.read_bytes().startswith(b"ISO-10303-21")
    assert out["path"] == f"fab/3d/{path.name}" and out["bytes"] == path.stat().st_size
    assert out["models"] == "none" and out["note"] == fab_tool.BARE_BOARD_NOTE
    model = await board_model(fab_rig.turn("run-3d-step-hash"))
    from copperchat.board.model import board_hash

    assert out["board_hash"] == board_hash(model)
    assert _leftovers(fab_rig) == []
    record_property("step_bytes", out["bytes"])
    record_property("step_s", out["seconds"])


async def test_glb(fab_rig: PlacedSynth, record_property: Any) -> None:
    out, err = await _export(fab_rig, "glb", "run-3d-glb")
    assert not err, out
    assert _out(fab_rig, "glb").read_bytes()[:4] == b"glTF"
    assert _leftovers(fab_rig) == []
    record_property("glb_bytes", out["bytes"])


async def test_symlinked_fab_refused(fab_rig: PlacedSynth, tmp_path: Path) -> None:
    _clean(fab_rig)
    outside = tmp_path / "outside"
    (outside / "3d.old-0123456789ab").mkdir(parents=True)
    (outside / "3d.old-0123456789ab" / "keep.step").write_text("keep")
    _fab(fab_rig).symlink_to(outside)
    try:
        out, err = await _export(fab_rig, "step", "run-3d-symlink")
        assert err and out["error"]["code"] == "output_path_unsafe", out
        assert (outside / "3d.old-0123456789ab" / "keep.step").read_text() == "keep"
        assert sorted(p.name for p in outside.iterdir()) == ["3d.old-0123456789ab"]
    finally:
        _clean(fab_rig)


async def test_failure_leaves_old_file(
    fab_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, err = await _export(fab_rig, "step", "run-3d-old-1")
    assert not err, out
    before = _out(fab_rig, "step").read_bytes()

    async def failing(settings: Any, args: Any, *, timeout: float, cwd: Any = None) -> Any:
        return CliResult(1, "", "simulated failure")

    monkeypatch.setattr(fab_tool, "run_kicad_cli", failing)
    out, err = await _export(fab_rig, "step", "run-3d-old-2")
    assert err and out["error"]["code"] == "export_failed", out
    assert _out(fab_rig, "step").read_bytes() == before
    assert _leftovers(fab_rig) == []


async def test_kicad_data_untouched(fab_rig: PlacedSynth) -> None:
    pro = fab_rig.session.project.pro_file
    files = [pro.with_suffix(s) for s in (".kicad_pcb", ".kicad_pro", ".kicad_dru")]
    before = [_sha(p) for p in files]
    modified = set(fab_rig.session.modified_docs)
    row = await fab_rig.db.fetchone("SELECT COUNT(*) FROM changes")
    ctx = fab_rig.turn("run-3d-untouched")
    out, err = await call_tool(ctx, "pcb_export_3d", format="glb")
    assert not err, out
    assert [_sha(p) for p in files] == before
    assert fab_rig.session.modified_docs == modified
    assert await fab_rig.db.fetchone("SELECT COUNT(*) FROM changes") == row
    assert ctx.editing is not None
    assert ctx.editing.turn(ctx.project.id, ctx.run_id).logical_used == 0


async def test_export_3d_under_cli_lock(
    fab_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = fab_tool.run_kicad_cli
    seen: list[tuple[tuple[str, ...], bool, float]] = []

    async def spy(settings: Any, args: Any, *, timeout: float, cwd: Any = None) -> Any:
        seen.append((tuple(args[:3]), BOARD_CLI_LOCK.locked(), timeout))
        return await real(settings, args, timeout=timeout, cwd=cwd)

    monkeypatch.setattr(fab_tool, "run_kicad_cli", spy)
    out, err = await _export(fab_rig, "step", "run-3d-lock")
    assert not err, out
    assert seen == [(("pcb", "export", "step"), True, 180)]


async def test_export_3d_holds_edit_lock(
    fab_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await board_model(fab_rig.turn("run-3d-el-model"))
    hole = next(p for p in sorted(model.parts, key=lambda p: p.ref) if p.ref.startswith("H"))
    ctx = fab_rig.turn("run-3d-el")
    move_ctx = dataclasses.replace(ctx, run_id="run-3d-el-move")  # the same edit lock
    real = fab_tool.run_kicad_cli
    gate = asyncio.Event()
    started = asyncio.Event()

    async def waiting(settings: Any, args: Any, *, timeout: float, cwd: Any = None) -> Any:
        started.set()
        await gate.wait()
        return await real(settings, args, timeout=timeout, cwd=cwd)

    monkeypatch.setattr(fab_tool, "run_kicad_cli", waiting)
    export = asyncio.ensure_future(call_tool(ctx, "pcb_export_3d", format="glb"))
    await asyncio.wait_for(started.wait(), 120)
    if move_ctx.on_mutation is not None:
        await move_ctx.on_mutation()
    move = asyncio.ensure_future(
        call_tool(move_ctx, "pcb_move", items=[{"ref": hole.ref, "x": hole.x + 1.0}])
    )
    await asyncio.sleep(1.0)
    assert not move.done(), "pcb_move did not wait for the 3D export's edit lock"
    gate.set()
    out, err = await export
    assert not err, out
    out2, err2 = await move
    assert not err2, out2
    await setup_call(
        fab_rig.turn("run-3d-el-back"), "pcb_move", items=[{"ref": hole.ref, "x": hole.x}]
    )
