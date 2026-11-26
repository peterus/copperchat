"""`pcb_fab_export` on the real api-server (SP4 P8 Task 3): the JLCPCB package of the routed,
poured `sp4_synth` (`fab_rig` = P5b's `routed_synth` via `copper_rig` + the default GND
stack), the approval / export-time metrics gates, the CLI lock, the two-step swap, the
leftover sweep and the symlink refusals.

The board approval is DB setup (`approve_board`): P3b tests the guards. The routed synth has
only its critical nets routed (no Freerouting fill before P6b), so its real metrics have hard
failures (unrouted nets): the happy-path tests use `metrics_pass`, which runs the real
`run_cpu(metrics_job, ...)` and reports its result with `hard_failures = 0`;
`test_export_metrics_hard_failures_refused` checks the real gate."""

from __future__ import annotations

import asyncio
import csv
import dataclasses
import hashlib
import io
import shutil
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from copperchat.board import worker
from copperchat.board.classify import classify
from copperchat.board.jobs import metrics_job
from copperchat.board.model import board_hash
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.board import fab as fab_tool
from copperchat.kicad_tools.board.read import board_model
from copperchat.views.board_render import BOARD_CLI_LOCK
from tests.kicad.board.conftest import PlacedSynth, approve_board, open_setup_db, setup_call
from tests.support.edit import call_tool

pytestmark = [
    pytest.mark.kicad,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.timeout(600),  # the first test of a shard builds the session rig
]

PRESET = {"preset": "jlcpcb"}


@pytest.fixture
def metrics_pass(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[Any, Any]]]:
    """The export's metrics through the real `run_cpu(metrics_job, ...)`, reported with no
    hard failure (the partly routed rig fails M2 etc.); yields the recorded (fn, kwargs)."""
    calls: list[tuple[Any, Any]] = []
    real = worker.run_cpu

    async def run_cpu(fn: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append((fn, kwargs))
        report = await real(fn, *args, **kwargs)
        if fn is metrics_job:
            report = dataclasses.replace(report, hard_failures=0)
        return report

    monkeypatch.setattr(fab_tool, "run_cpu", run_cpu)
    yield calls


def _fab(rig: PlacedSynth) -> Path:
    return rig.session.project.dir / "fab"


def _target(rig: PlacedSynth) -> Path:
    return _fab(rig) / "jlcpcb"


def _tree(d: Path) -> dict[str, bytes]:
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def _clean_fab(rig: PlacedSynth) -> None:
    fab = _fab(rig)
    if fab.is_symlink() or fab.is_file():
        fab.unlink()
    elif fab.exists():
        shutil.rmtree(fab)


def _no_leftovers(rig: PlacedSynth) -> None:
    names = [p.name for p in _fab(rig).iterdir()]
    assert not [n for n in names if ".tmp-" in n or ".old-" in n], names


async def _export(rig: PlacedSynth, run: str) -> tuple[dict[str, Any], bool]:
    return await call_tool(rig.turn(run), "pcb_fab_export", **PRESET)


def _ok(out: dict[str, Any], err: bool) -> dict[str, Any]:
    assert not err, out
    return out


async def _attributes(rig: PlacedSynth) -> dict[str, tuple[bool, bool]]:
    def read(h: KicadHandle) -> dict[str, tuple[bool, bool]]:
        return {
            str(fp.reference_field.text.value): (
                bool(fp.attributes.do_not_populate),
                bool(fp.attributes.exclude_from_position_files),
            )
            for fp in h.board().get_footprints()
        }

    return await rig.session.call(read, timeout=60)


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _kicad_files(rig: PlacedSynth) -> dict[str, str | None]:
    pro = rig.session.project.pro_file
    return {s: _sha(pro.with_suffix(s)) for s in (".kicad_pcb", ".kicad_pro", ".kicad_dru")}


async def _changes(rig: PlacedSynth) -> int:
    row = await rig.db.fetchone("SELECT COUNT(*) FROM changes")
    assert row is not None
    return int(row[0])


async def _hole(rig: PlacedSynth) -> Any:
    model = await board_model(rig.turn("run-fab-hole"))
    return next(p for p in sorted(model.parts, key=lambda p: p.ref) if p.ref.startswith("H"))


async def _mounting_holes(rig: PlacedSynth) -> set[str]:
    model = await board_model(rig.turn("run-fab-classify"))
    cls = classify(model)
    return {ref for ref, kind in cls.part_class.items() if kind == "mounting_hole"}


async def _move_hole(rig: PlacedSynth, ref: str, x: float, run: str) -> None:
    await setup_call(rig.turn(run), "pcb_move", items=[{"ref": ref, "x": x}])


async def test_file_set(
    fab_rig: PlacedSynth, metrics_pass: list[Any], record_property: Any
) -> None:
    _clean_fab(fab_rig)
    digest = await approve_board(fab_rig)
    t0 = time.monotonic()
    data = _ok(*await _export(fab_rig, "run-fab-files"))
    seconds = time.monotonic() - t0
    target = _target(fab_rig)
    stem = fab_rig.session.project.pro_file.stem
    gerbers = sorted(p.name for p in (target / "gerbers").iterdir())
    copper = [g for g in gerbers if g.endswith((".gtl", ".gbl", ".g1", ".g2"))]
    assert len(copper) == 4, gerbers
    for part in ("F_Mask", "B_Mask", "F_Silkscreen", "B_Silkscreen", "F_Paste", "B_Paste",
                 "Edge_Cuts"):  # fmt: skip
        assert any(part in g for g in gerbers), (part, gerbers)
    assert len([g for g in gerbers if g.endswith(".gbrjob")]) == 1
    drill = sorted(p.name for p in (target / "drill").iterdir())
    pth = target / "drill" / f"{stem}-PTH.drl"
    npth = target / "drill" / f"{stem}-NPTH.drl"
    assert pth.exists() and npth.exists(), drill
    assert "METRIC" in pth.read_text() and "METRIC" in npth.read_text()
    assert any("map" in d for d in drill), drill
    with zipfile.ZipFile(target / f"{stem}-jlcpcb.zip") as zf:
        assert sorted(zf.namelist()) == sorted(gerbers + drill)
        assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in zf.infolist())
    bom = (target / "bom-jlcpcb.csv").read_text()
    assert bom.splitlines()[0] == "Comment,Designator,Footprint,LCSC Part #"
    bom_rows = list(csv.DictReader(io.StringIO(bom)))
    bom_refs = [r for row in bom_rows for r in row["Designator"].split(",")]
    assert len(bom_refs) == len(set(bom_refs)) and all(bom_refs)
    assert not [r for r in bom_refs if "-" in r]  # no "C1-C3" ranges
    assert all(":" not in row["Footprint"] for row in bom_rows)  # no library prefix
    cpl_text = (target / "cpl-jlcpcb.csv").read_text()
    assert cpl_text.splitlines()[0] == "Designator,Mid X,Mid Y,Layer,Rotation"
    cpl = list(csv.DictReader(io.StringIO(cpl_text)))
    attrs = await _attributes(fab_rig)
    holes = await _mounting_holes(fab_rig)
    expected = {
        ref for ref, (dnp, no_pos) in attrs.items() if not dnp and not no_pos and ref not in holes
    }
    assert {r["Designator"] for r in cpl} == expected
    # every BOM part is placed (CPL) or a part without a position file entry; no DNP, no hole
    assert set(bom_refs) <= set(attrs) and not {r for r in bom_refs if attrs[r][0]}  # no DNP
    # the tool-placed mounting holes (P2 pcb_set_outline: excluded from position files) are
    # absent
    tool_holes = {ref for ref, (_, no_pos) in attrs.items() if ref.startswith("H") and no_pos}
    assert len(tool_holes) >= 2 and not tool_holes & {r["Designator"] for r in cpl}
    assert all(r["Mid X"].endswith("mm") and r["Layer"] in ("Top", "Bottom") for r in cpl)
    report = (target / "fab-report.md").read_text()
    assert digest in report and "not validated for JLC assembly" in report
    assert data["board_hash"] == digest and data["dir"] == "fab/jlcpcb"
    listed = {f["path"]: f["bytes"] for f in data["files"]}
    assert set(listed) == set(_tree(target))
    assert all(listed[p] == (target / p).stat().st_size for p in listed)
    assert data["notes"][0].startswith("BOM/CPL not validated for JLC assembly")
    assert not (target / "src").exists()
    _no_leftovers(fab_rig)
    size = (target / f"{stem}-jlcpcb.zip").stat().st_size
    record_property("fab_export_s", round(seconds, 2))
    record_property("fab_zip_bytes", size)
    record_property("fab_package_bytes", sum(listed.values()))
    record_property("fab_gerbers", ",".join(gerbers))
    record_property("fab_drill", ",".join(drill))
    print(f"fab export {seconds:.1f} s, zip {size} B, files {sorted(listed)}")


async def test_fab_export_excludes_holes(fab_rig: PlacedSynth, metrics_pass: list[Any]) -> None:
    """R5 (spec §22.6): every part classified `mounting_hole` - sp4_synth's schematic holes
    without the exclude attributes included - is absent from the JLC BOM and CPL and listed
    in the report's "Excluded from BOM/CPL" section."""
    _clean_fab(fab_rig)
    await approve_board(fab_rig)
    _ok(*await _export(fab_rig, "run-fab-holes"))
    holes = await _mounting_holes(fab_rig)
    attrs = await _attributes(fab_rig)
    schematic_holes = {h for h in holes if not attrs[h][1]}
    assert len(holes) >= 4 and schematic_holes, (holes, attrs)
    target = _target(fab_rig)
    bom = list(csv.DictReader(io.StringIO((target / "bom-jlcpcb.csv").read_text())))
    cpl = list(csv.DictReader(io.StringIO((target / "cpl-jlcpcb.csv").read_text())))
    bom_refs = {r for row in bom for r in row["Designator"].split(",")}
    assert not holes & bom_refs and bom_refs
    assert not holes & {r["Designator"] for r in cpl} and cpl
    report = (target / "fab-report.md").read_text()
    section = report.split("## Excluded from BOM/CPL", 1)[1].split("\n## ", 1)[0]
    assert all(h in section for h in holes), section


async def test_report_metrics_via_run_cpu(
    fab_rig: PlacedSynth, metrics_pass: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from copperchat.board import metrics

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("compute_metrics called in-process")

    monkeypatch.setattr(metrics, "compute_metrics", forbidden)
    await approve_board(fab_rig)
    _ok(*await _export(fab_rig, "run-fab-runcpu"))
    jobs = [(fn, kw) for fn, kw in metrics_pass if fn is metrics_job]
    assert len(jobs) == 1
    assert jobs[0][1]["only"] is None and jobs[0][1]["timeout_s"] == 30
    report = (_target(fab_rig) / "fab-report.md").read_text()
    assert "| M11 |" in report and "| M14 |" in report


async def test_export_metrics_hard_failures_refused(fab_rig: PlacedSynth) -> None:
    """The real export-time metrics of the partly routed rig have hard failures: refused
    although the (DB) approval and full-check row say 0 - this is how a board.md edit after
    the approval (no change row) is caught. Nothing written."""
    _clean_fab(fab_rig)
    await approve_board(fab_rig)
    out, err = await _export(fab_rig, "run-fab-hard")
    assert err and out["error"]["code"] == "not_approved", out
    details = out["error"]["details"]
    assert details["reason"] == "check_failed" and details["hard_failures"] > 0
    assert details["failing"]
    assert not _target(fab_rig).exists()


async def test_fab_cli_under_lock(
    fab_rig: PlacedSynth, metrics_pass: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await approve_board(fab_rig)
    real = fab_tool.run_kicad_cli
    seen: list[tuple[str, bool, float]] = []
    gate = asyncio.Event()
    in_gerbers = asyncio.Event()
    pro_copies: list[bool] = []

    async def spy(settings: Any, args: Any, *, timeout: float, cwd: Any = None) -> Any:
        seen.append((args[2], BOARD_CLI_LOCK.locked(), timeout))
        if args[2] == "gerbers":
            # the copy's project file sits next to it (a --check-zones refill uses its rules)
            pro = fab_rig.session.project.pro_file
            copied = Path(cwd) / "src" / pro.name
            pro_copies.append(copied.read_bytes() == pro.read_bytes())
            in_gerbers.set()
            await gate.wait()
        return await real(settings, args, timeout=timeout, cwd=cwd)

    monkeypatch.setattr(fab_tool, "run_kicad_cli", spy)
    render_cli: list[float] = []
    from copperchat.views import board_render

    real_render_cli = board_render.run_kicad_cli

    async def render_spy(settings: Any, args: Any, *, timeout: float, cwd: Any = None) -> Any:
        render_cli.append(time.monotonic())
        return await real_render_cli(settings, args, timeout=timeout, cwd=cwd)

    monkeypatch.setattr(board_render, "run_kicad_cli", render_spy)
    export = asyncio.ensure_future(_export(fab_rig, "run-fab-lock"))
    await asyncio.wait_for(in_gerbers.wait(), 120)
    render = asyncio.ensure_future(
        call_tool(fab_rig.turn("run-fab-lock-render"), "pcb_render", view="top")
    )
    await asyncio.sleep(2.0)
    assert render_cli == [], "a render reached kicad-cli while the gerbers command ran"
    released = time.monotonic()
    gate.set()
    _ok(*await export)
    _ok(*await render)
    assert render_cli and render_cli[0] >= released
    assert [(s, locked) for s, locked, _ in seen] == [
        ("gerbers", True), ("drill", True), ("pos", True), ("bom", True)
    ]  # fmt: skip
    assert [t for _, _, t in seen] == [120, 60, 60, 60]
    assert pro_copies == [True]


async def test_kicad_data_untouched(fab_rig: PlacedSynth, metrics_pass: list[Any]) -> None:
    await approve_board(fab_rig)
    before = _kicad_files(fab_rig)
    modified = set(fab_rig.session.modified_docs)
    changes = await _changes(fab_rig)
    ctx = fab_rig.turn("run-fab-untouched")
    out, err = await call_tool(ctx, "pcb_fab_export", **PRESET)
    _ok(out, err)
    assert _kicad_files(fab_rig) == before
    assert fab_rig.session.modified_docs == modified
    assert await _changes(fab_rig) == changes  # no change-log entry
    assert ctx.editing is not None
    assert ctx.editing.turn(ctx.project.id, ctx.run_id).logical_used == 0  # cost 0


async def test_reexport_keeps_old_on_failure(
    fab_rig: PlacedSynth, metrics_pass: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_fab(fab_rig)
    await approve_board(fab_rig)
    _ok(*await _export(fab_rig, "run-fab-keep-1"))
    before = _tree(_target(fab_rig))

    def broken(tmp: Path, name: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(fab_tool, "zip_package", broken)
    out, err = await _export(fab_rig, "run-fab-keep-2")
    assert err and out["error"]["code"] == "export_failed", out
    assert _tree(_target(fab_rig)) == before
    _no_leftovers(fab_rig)


async def test_reexport_replaces(fab_rig: PlacedSynth, metrics_pass: list[Any]) -> None:
    await approve_board(fab_rig)
    _ok(*await _export(fab_rig, "run-fab-re-1"))
    first = (_target(fab_rig) / "fab-report.md").read_text()
    await asyncio.sleep(1.1)  # the report's date has second resolution at least
    _ok(*await _export(fab_rig, "run-fab-re-2"))
    second = (_target(fab_rig) / "fab-report.md").read_text()
    assert first != second
    _no_leftovers(fab_rig)


async def test_leftover_swept(fab_rig: PlacedSynth, metrics_pass: list[Any]) -> None:
    await approve_board(fab_rig)
    if not _target(fab_rig).exists():  # with the target present the .old- dir is swept
        _ok(*await _export(fab_rig, "run-fab-swept-0"))
    fab = _fab(fab_rig)
    for name in (".jlcpcb.tmp-0123456789ab", "jlcpcb.old-0123456789ab"):
        (fab / name).mkdir()
        (fab / name / "crashed.txt").write_text("left by a killed export")
    data = _ok(*await _export(fab_rig, "run-fab-swept"))
    assert data["swept"] == [".jlcpcb.tmp-0123456789ab", "jlcpcb.old-0123456789ab"]
    report = (_target(fab_rig) / "fab-report.md").read_text()
    assert "Swept leftovers" in report and "jlcpcb.old-0123456789ab" in report
    _no_leftovers(fab_rig)


async def test_symlinked_fab_refused(
    fab_rig: PlacedSynth, metrics_pass: list[Any], tmp_path: Path
) -> None:
    await approve_board(fab_rig)
    _clean_fab(fab_rig)
    outside = tmp_path / "outside"
    outside.mkdir()
    _fab(fab_rig).symlink_to(outside)
    try:
        out, err = await _export(fab_rig, "run-fab-symlink")
        assert err and out["error"]["code"] == "output_path_unsafe", out
        assert out["error"]["details"]["reason"] == "symlink"
        assert list(outside.iterdir()) == []
    finally:
        _clean_fab(fab_rig)


async def test_symlinked_fab_with_leftover_refused(
    fab_rig: PlacedSynth, metrics_pass: list[Any], tmp_path: Path
) -> None:
    await approve_board(fab_rig)
    _clean_fab(fab_rig)
    outside = tmp_path / "outside"
    (outside / "jlcpcb.old-0123456789ab").mkdir(parents=True)
    (outside / "jlcpcb.old-0123456789ab" / "precious.txt").write_text("keep")
    _fab(fab_rig).symlink_to(outside)
    try:
        out, err = await _export(fab_rig, "run-fab-symlink-left")
        assert err and out["error"]["code"] == "output_path_unsafe", out
        assert (outside / "jlcpcb.old-0123456789ab" / "precious.txt").read_text() == "keep"
        assert sorted(p.name for p in outside.iterdir()) == ["jlcpcb.old-0123456789ab"]
    finally:
        _clean_fab(fab_rig)


async def test_symlinked_target_refused(fab_rig: PlacedSynth, metrics_pass: list[Any]) -> None:
    await approve_board(fab_rig)
    _clean_fab(fab_rig)
    (_fab(fab_rig) / "other").mkdir(parents=True)
    _target(fab_rig).symlink_to(_fab(fab_rig) / "other")
    try:
        out, err = await _export(fab_rig, "run-fab-symlink-target")
        assert err and out["error"]["code"] == "output_path_unsafe", out
        assert list((_fab(fab_rig) / "other").iterdir()) == []
    finally:
        _clean_fab(fab_rig)


async def test_not_approved_refused(fab_rig: PlacedSynth, tmp_path: Path) -> None:
    fresh = await open_setup_db(tmp_path / "fresh.db", fab_rig.session.project.id)
    try:
        ctx = fab_rig.rig.turn(fresh, "run-fab-na")
        out, err = await call_tool(ctx, "pcb_fab_export", **PRESET)
        assert err and out["error"]["code"] == "not_approved", out
        assert out["error"]["details"]["reason"] == "missing"
    finally:
        await fresh.close()


async def test_hard_failure_check_refused(fab_rig: PlacedSynth, metrics_pass: list[Any]) -> None:
    await approve_board(fab_rig, hard_failures=2)
    out, err = await _export(fab_rig, "run-fab-hardrow")
    assert err and out["error"]["code"] == "not_approved", out
    assert out["error"]["details"]["reason"] == "check_failed"


async def test_stale_approval_refused(fab_rig: PlacedSynth, metrics_pass: list[Any]) -> None:
    """A move after the approval (a mounting hole: no copper on it, so the shared rig's
    routing stays; plan: `pcb_move … unroute: true` of any part) → `not_approved` stale;
    moved back exactly afterwards."""
    digest = await approve_board(fab_rig)
    hole = await _hole(fab_rig)
    await _move_hole(fab_rig, hole.ref, hole.x + 1.0, "run-fab-stale-1")
    try:
        out, err = await _export(fab_rig, "run-fab-stale-2")
        assert err and out["error"]["code"] == "not_approved", out
        assert out["error"]["details"]["reason"] == "stale"
    finally:
        await _move_hole(fab_rig, hole.ref, hole.x, "run-fab-stale-3")
    assert board_hash(await board_model(fab_rig.turn("run-fab-stale-4"))) == digest


async def test_concurrent_move_blocks_or_refuses(
    fab_rig: PlacedSynth, metrics_pass: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """(1) A `pcb_move` through the adapter while the export runs waits for the edit lock;
    the package carries the approved hash, then the move lands. (2) A write that bypassed the
    lock between the copy and its read-back → `not_approved` changed_during_export, the old
    package untouched, no staging dir left."""
    _clean_fab(fab_rig)
    digest = await approve_board(fab_rig)
    hole = await _hole(fab_rig)
    ctx = fab_rig.turn("run-fab-conc")
    move_ctx = dataclasses.replace(ctx, run_id="run-fab-conc-move")  # the same edit lock
    real = fab_tool.run_kicad_cli
    gate = asyncio.Event()
    started = asyncio.Event()

    async def waiting(settings: Any, args: Any, *, timeout: float, cwd: Any = None) -> Any:
        if not started.is_set():
            started.set()
            await gate.wait()
        return await real(settings, args, timeout=timeout, cwd=cwd)

    monkeypatch.setattr(fab_tool, "run_kicad_cli", waiting)
    export = asyncio.ensure_future(call_tool(ctx, "pcb_fab_export", **PRESET))
    await asyncio.wait_for(started.wait(), 120)
    if move_ctx.on_mutation is not None:
        await move_ctx.on_mutation()
    move = asyncio.ensure_future(
        call_tool(move_ctx, "pcb_move", items=[{"ref": hole.ref, "x": hole.x + 1.0}])
    )
    await asyncio.sleep(1.0)
    assert not move.done(), "pcb_move did not wait for the export's edit lock"
    gate.set()
    data = _ok(*await export)
    assert data["board_hash"] == digest
    assert digest in (_target(fab_rig) / "fab-report.md").read_text()
    _ok(*await move)
    await _move_hole(fab_rig, hole.ref, hole.x, "run-fab-conc-back")
    assert board_hash(await board_model(fab_rig.turn("run-fab-conc-check"))) == digest
    monkeypatch.setattr(fab_tool, "run_kicad_cli", real)

    # (2) a write that bypassed the lock while save_copy ran
    await approve_board(fab_rig)
    before = _tree(_target(fab_rig))
    session = fab_rig.session
    real_copy = session.save_copy
    from kipy.geometry import Vector2

    def nudge(dx: float) -> Any:
        def run(h: KicadHandle) -> None:
            board = h.board()
            fp = next(f for f in board.get_footprints() if f.reference_field.text.value == hole.ref)
            fp.position = Vector2.from_xy_mm(hole.x + dx, hole.y)
            board.update_items([fp])

        return run

    async def sneaky(doc: Any, dest: Path, **kwargs: Any) -> Path:
        out = await real_copy(doc, dest, **kwargs)
        await session.call(nudge(0.7), timeout=60)
        return out

    monkeypatch.setattr(session, "save_copy", sneaky)
    try:
        out, err = await _export(fab_rig, "run-fab-sneaky")
        assert err and out["error"]["code"] == "not_approved", out
        assert out["error"]["details"]["reason"] == "changed_during_export"
        assert _tree(_target(fab_rig)) == before
        _no_leftovers(fab_rig)
    finally:
        monkeypatch.setattr(session, "save_copy", real_copy)
        await session.call(nudge(0.0), timeout=60)
    assert board_hash(await board_model(fab_rig.turn("run-fab-sneaky-check"))) == digest


async def test_paste_layer_matches_smd_pads(
    fab_rig: PlacedSynth, metrics_pass: list[Any], record_property: Any
) -> None:
    """Stencil (owner: hot-plate reflow): F.Paste has one flash per pad with F.Paste in the
    board (every top SMD pad, paste-only sub-pads included), nothing for THT / NPTH pads; B.Paste
    is empty (all SMD parts on top)."""
    await approve_board(fab_rig)
    _ok(*await _export(fab_rig, "run-fab-paste"))
    model = await board_model(fab_rig.turn("run-fab-paste-model"))
    stem = fab_rig.session.project.pro_file.stem
    gerbers = _target(fab_rig) / "gerbers"

    def flashes(path: Path) -> int:
        text = path.read_text()
        return text.count("D03*") + text.count("G36*")

    top = [q for p in model.parts for q in p.pads if "F.Paste" in q.layers]
    bottom = [q for p in model.parts for q in p.pads if "B.Paste" in q.layers]
    assert top and all(q.smd or not ({"F.Cu", "B.Cu"} & q.layers) for q in top)
    assert flashes(gerbers / f"{stem}-F_Paste.gtp") == len(top)
    assert flashes(gerbers / f"{stem}-B_Paste.gbp") == len(bottom) == 0
    record_property("paste_flashes_top", len(top))
