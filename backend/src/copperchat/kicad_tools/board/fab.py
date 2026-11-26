"""`pcb_fab_export` and `pcb_export_3d` (SP4 P8, spec §7.2 "Checkpoints and output", §14).

Both are plain `ToolSpec`s (index call 7): they write no KiCad data, cost 0 and log no change
entry; their output lives in `<project>/fab/` (a normal repo directory, never snapshotted,
spec §6.4). Each runs **entirely under the project's edit lock** (the lock every two-phase
write holds from prepare to the budget record), so no board write lands between the
approval check and the board copy the package is made from. Every `kicad-cli` call runs
inside `BOARD_CLI_LOCK` (taken per command) with an explicit timeout.

`pcb_fab_export {preset: "jlcpcb"}`: the board approval (`require_board_approved`) **and**
the metrics re-run at export time (`run_cpu(metrics_job, …)` with a fresh DRC and the
current `board.md`) must hold - a `board.md` edit after the approval (preset, Checks rows)
logs no change row, so only re-running the metrics catches it (P3b residual). The package is
built in `fab/.jlcpcb.tmp-<id>/` from a `save_copy` of the board (never the live file; the
copy's `board_hash` must still be the approved one), then swapped in with two renames (the
old package survives every failure).

`pcb_export_3d {format: step|glb}`: no approval; the same safe path helper, a copy, one
`kicad-cli pcb export step|glb` and an atomic file replace in `fab/3d/`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import stat
import time
import zipfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from copperchat.board import fab as plan
from copperchat.board.boardmd import read_board_doc
from copperchat.board.jobs import metrics_job
from copperchat.board.metrics import MetricsReport
from copperchat.board.model import BoardModel, board_hash
from copperchat.board.paths import (
    export_failed,
    new_call_id,
    replace_file,
    safe_output_dir,
    staging_dir,
    swap_in,
    sweep_stale,
)
from copperchat.board.worker import run_cpu
from copperchat.db import utcnow_iso
from copperchat.db.repos.board import board_file
from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.board import api
from copperchat.kicad_tools.board.analysis import preset_for
from copperchat.kicad_tools.board.checkpoints import require_board_approved
from copperchat.kicad_tools.board.read import board_model, require_board
from copperchat.kicad_tools.board.setup import require_board_tools
from copperchat.kicad_tools.cli import run_kicad_cli
from copperchat.tools.spec import ToolContext, ToolResult
from copperchat.views.board_render import BOARD_CLI_LOCK

log = logging.getLogger(__name__)

PRESETS = ("jlcpcb",)
FORMATS = ("step", "glb")
FAB_TARGET = "fab/jlcpcb"
TARGET_3D = "fab/3d"
FAB_DRC_TIMEOUT_S = 60.0
FAB_METRICS_TIMEOUT_S = 30.0
MAX_CSV_BYTES = 8 * 1024 * 1024  # kicad-cli's pos / bom CSV read back (thousands of parts fit)
ZIP_DATE = (1980, 1, 1, 0, 0, 0)  # fixed timestamps: the same files give the same zip
BARE_BOARD_NOTE = (
    "bare board: this image has no KiCad 3D part models (gap G7; optional phase P10 adds them)"
)
NEXT_FAB = "offer a git commit of fab/ (spec §7.2: fab/ is a normal repo directory)"


def _invalid(what: str, why: str) -> StructuredToolError:
    return StructuredToolError(what, why, "see the tool schema", code="invalid_argument")


def _choice(args: dict[str, Any], key: str, allowed: tuple[str, ...]) -> str:
    value = args.get(key)
    if not isinstance(value, str) or value not in allowed:
        raise _invalid(f"Invalid {key}", f"{key} must be one of {', '.join(allowed)}")
    extra = sorted(set(args) - {key})
    if extra:
        raise _invalid("Unknown argument", f"unexpected {', '.join(extra)}")
    return value


@contextlib.asynccontextmanager
async def _edit_lock(ctx: ToolContext) -> AsyncIterator[None]:
    """The project's edit lock when an editing service is attached (the run engine always
    has one), else nothing to lock against."""
    if ctx.editing is None:
        yield
        return
    async with ctx.editing.edit_lock(ctx.project.id):
        yield


async def _off_loop[T](fn: Callable[[], T]) -> T:
    """`fn` in a thread, shielded: a cancel waits for the thread (no file operation is cut
    off half-way) and then propagates."""
    work = asyncio.ensure_future(asyncio.to_thread(fn))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            await work
        raise


async def _remove(path: Path) -> None:
    await _off_loop(lambda: shutil.rmtree(path, ignore_errors=True))


async def _cli(ctx: ToolContext, cmd: plan.FabCommand, cwd: Path) -> None:
    """One fab `kicad-cli` call under `BOARD_CLI_LOCK` (per command, so a render waits at
    most one command) with the command's explicit timeout."""
    what = " ".join(cmd.args[:3])
    async with BOARD_CLI_LOCK:
        try:
            result = await run_kicad_cli(ctx.settings, cmd.args, timeout=cmd.timeout_s, cwd=cwd)
        except TimeoutError:
            why = f"kicad-cli {what} took longer than {cmd.timeout_s:.0f} s"
            raise export_failed(why) from None
        except OSError as exc:
            raise export_failed(f"kicad-cli could not be started ({type(exc).__name__})") from None
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip()[-400:]
        raise export_failed(f"kicad-cli {what} exited with {result.returncode}: {tail}")


def _read_small(path: Path, max_bytes: int = MAX_CSV_BYTES) -> str:
    """A kicad-cli output read back as text: a regular file (no symlink) of at most
    `max_bytes`."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            raise ValueError(f"{path.name} is not a regular file of at most {max_bytes} bytes")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return fh.read(max_bytes + 1).decode("utf-8-sig", errors="replace")
    finally:
        os.close(fd)


def _regular_files(d: Path) -> list[Path]:
    """Regular files under `d` (sorted, posix relative order); symlinks are never followed."""
    out: list[Path] = []
    for root, dirs, files in os.walk(d, followlinks=False):
        dirs.sort()
        for name in sorted(files):
            p = Path(root) / name
            if stat.S_ISREG(os.lstat(p).st_mode):
                out.append(p)
    return out


def zip_package(tmp: Path, zip_name: str) -> None:
    """`<board>-jlcpcb.zip` of every file in `gerbers/` and `drill/` (flat names, sorted,
    deflated, fixed timestamps) - the upload for JLCPCB."""
    members: dict[str, Path] = {}
    for sub in (plan.GERBER_DIR, plan.DRILL_DIR):
        for p in _regular_files(tmp / sub):
            if p.name in members:
                raise ValueError(f"two outputs named {p.name}")
            members[p.name] = p
    if not members:
        raise ValueError("kicad-cli wrote no gerber or drill files")
    with zipfile.ZipFile(tmp / zip_name, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, members[name].read_bytes())


def _write_csvs(tmp: Path) -> None:
    """kicad-cli's pos / BOM CSV → `cpl-jlcpcb.csv` / `bom-jlcpcb.csv`; the raw files are
    removed (they are not part of the package)."""
    cpl = plan.cpl_rows(_read_small(tmp / plan.POS_CSV))
    bom = plan.bom_rows(_read_small(tmp / plan.BOM_CSV))
    (tmp / plan.CPL_FILE).write_text(plan.csv_text(plan.CPL_HEADER, cpl), encoding="utf-8")
    (tmp / plan.BOM_FILE).write_text(plan.csv_text(plan.BOM_HEADER, bom), encoding="utf-8")
    (tmp / plan.POS_CSV).unlink()
    (tmp / plan.BOM_CSV).unlink()


def _file_list(d: Path) -> list[tuple[str, int]]:
    return [(p.relative_to(d).as_posix(), os.lstat(p).st_size) for p in _regular_files(d)]


def _not_approved(reason: str, digest: str, why: str, **extra: Any) -> StructuredToolError:
    return StructuredToolError(
        "The board is not approved",
        why,
        "Run pcb_check full, render top and bottom, show them to the user and end the turn; "
        "after their OK call pcb_approve stage 'board', then export again.",
        code="not_approved",
        details={"reason": reason, "board_hash": digest, **extra},
    )


async def _export_metrics(ctx: ToolContext, model: BoardModel) -> tuple[MetricsReport, Any]:
    """M1-M14 of the approved board with a fresh DRC and the current `board.md` (as the full
    check computes them, without the parity run): (report, board doc)."""
    doc = await read_board_doc(ctx)
    async with ctx.session.applying():
        drc = await ctx.session.call(
            lambda h: api.run_drc_task(h, timeout_s=FAB_DRC_TIMEOUT_S),
            timeout=api.drc_call_timeout(FAB_DRC_TIMEOUT_S),
        )
    report = await run_cpu(
        metrics_job,
        model,
        doc,
        drc,
        preset_for(model, doc),
        only=None,
        timeout_s=FAB_METRICS_TIMEOUT_S,
    )
    return report, doc


def _os_failed(exc: OSError) -> StructuredToolError:
    return export_failed(f"a package file could not be written ({type(exc).__name__}: {exc})")


async def _copy_board(ctx: ToolContext, dest: Path) -> BoardModel:
    """`save_copy` of the live board to `dest` (the copy only, never a save) and the model
    read right after it (the copied state: the edit lock is held)."""
    await ctx.session.save_copy("pcb", dest, save_modified=False)
    return await board_model(ctx)


async def pcb_fab_export(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    _choice(args, "preset", PRESETS)
    require_board_tools(ctx)
    require_board(ctx)
    async with _edit_lock(ctx):
        return await _fab_export(ctx)


async def _fab_export(ctx: ToolContext) -> ToolResult:
    start = time.monotonic()
    model = await board_model(ctx)
    await require_board_approved(ctx, model)
    digest = await asyncio.to_thread(board_hash, model)
    report, doc = await _export_metrics(ctx, model)
    if report.hard_failures:
        failing = [m.id for m in report.metrics if m.verdict == "fail"]
        raise _not_approved(
            "check_failed",
            digest,
            f"the metrics at export time have {report.hard_failures} hard failure(s) "
            f"({', '.join(failing)}) - e.g. board.md changed after the approval",
            hard_failures=report.hard_failures,
            failing=failing,
        )
    project = ctx.project
    target = await asyncio.to_thread(safe_output_dir, project.dir, FAB_TARGET)
    try:
        swept = await _off_loop(lambda: sweep_stale(target.parent, target.name))
    except OSError as exc:
        raise _os_failed(exc) from None
    call_id = new_call_id()
    try:
        tmp = await asyncio.to_thread(staging_dir, target, call_id)
    except OSError as exc:
        raise _os_failed(exc) from None
    stem = project.pcb_file.stem if project.pcb_file is not None else project.name
    try:
        src = tmp / "src" / f"{stem}.kicad_pcb"
        try:
            copy_model = await _copy_board(ctx, src)
        except OSError as exc:
            raise _os_failed(exc) from None
        if await asyncio.to_thread(board_hash, copy_model) != digest:
            raise _not_approved(
                "changed_during_export",
                digest,
                "the board changed while the export copied it (a write that bypassed the "
                "edit lock); nothing was exported",
            )
        for cmd in plan.fab_commands(model, src.relative_to(tmp).as_posix()):
            if cmd.out in (plan.GERBER_DIR, plan.DRILL_DIR):
                await asyncio.to_thread((tmp / cmd.out).mkdir)
            await _cli(ctx, cmd, tmp)
        zip_name = f"{stem}-jlcpcb.zip"
        try:
            await _off_loop(lambda: zip_package(tmp, zip_name))
            await _off_loop(lambda: _write_csvs(tmp))
            await _off_loop(lambda: shutil.rmtree(tmp / "src"))
            files = await _off_loop(lambda: _file_list(tmp))
        except ValueError as exc:
            raise export_failed(str(exc)) from None
        except OSError as exc:
            raise _os_failed(exc) from None
        text = plan.fab_report(
            model,
            board_file=board_file(project),
            board_hash=digest,
            date=utcnow_iso(),
            preset=preset_for(model, doc),
            metrics=report,
            accepted=doc.accepted,
            custom_rules=plan.custom_rules(model.dru_text),
            files=files,
            swept=swept,
        )
        try:
            await _off_loop(lambda: (tmp / plan.REPORT_FILE).write_text(text, encoding="utf-8"))
            files = await _off_loop(lambda: _file_list(tmp))
            warning = await _off_loop(lambda: swap_in(tmp, target, call_id))
        except OSError as exc:
            raise _os_failed(exc) from None
    except BaseException:
        await _remove(tmp)
        raise
    notes = [plan.ASSEMBLY_NOTE]
    if warning:
        notes.append(warning)
    data: dict[str, Any] = {
        "dir": FAB_TARGET,
        "files": [{"path": p, "bytes": n} for p, n in files],
        "board_hash": digest,
        "preset": getattr(preset_for(model, doc), "id", None),
        "hard_failures": report.hard_failures,
        "reviews": report.reviews,
        "swept": swept,
        "notes": notes,
        "next": NEXT_FAB,
        "seconds": round(time.monotonic() - start, 3),
    }
    return ToolResult(data=data)


async def pcb_export_3d(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    fmt = _choice(args, "format", FORMATS)
    require_board_tools(ctx)
    require_board(ctx)
    async with _edit_lock(ctx):
        return await _export_3d(ctx, fmt)


def _target_3d(project_dir: Path) -> Path:
    target = safe_output_dir(project_dir, TARGET_3D)
    if not os.path.lexists(target):
        target.mkdir()
    return safe_output_dir(project_dir, TARGET_3D)  # re-validated after the mkdir


async def _export_3d(ctx: ToolContext, fmt: str) -> ToolResult:
    start = time.monotonic()
    project = ctx.project
    model = await board_model(ctx)
    digest = await asyncio.to_thread(board_hash, model)
    try:
        target = await asyncio.to_thread(_target_3d, project.dir)
        swept = await _off_loop(lambda: sweep_stale(target.parent, target.name))
        tmp = await asyncio.to_thread(staging_dir, target, new_call_id())
    except OSError as exc:
        raise _os_failed(exc) from None
    stem = project.pcb_file.stem if project.pcb_file is not None else project.name
    name = f"{stem}.{fmt}"
    try:
        src = tmp / "src" / f"{stem}.kicad_pcb"
        try:
            copy_model = await _copy_board(ctx, src)
        except OSError as exc:
            raise _os_failed(exc) from None
        copied = await asyncio.to_thread(board_hash, copy_model)
        if copied != digest:
            raise export_failed("the board changed while the export copied it")
        cmd = plan.FabCommand(
            ("pcb", "export", fmt, "--force", "-o", name, src.relative_to(tmp).as_posix()),
            name,
            plan.EXPORT_3D_TIMEOUT_S,
        )
        await _cli(ctx, cmd, tmp)
        try:
            size = await _off_loop(lambda: _check_output(tmp / name, fmt))
            await _off_loop(lambda: replace_file(tmp / name, target / name))
        except ValueError as exc:
            raise export_failed(str(exc)) from None
        except OSError as exc:
            raise _os_failed(exc) from None
    finally:
        await _remove(tmp)
    return ToolResult(
        data={
            "path": f"{TARGET_3D}/{name}",
            "bytes": size,
            "board_hash": copied,
            "models": "none",
            "note": BARE_BOARD_NOTE,
            "swept": swept,
            "seconds": round(time.monotonic() - start, 3),
        }
    )


_MAGIC = {"step": b"ISO-10303-21", "glb": b"glTF"}


def _check_output(path: Path, fmt: str) -> int:
    """kicad-cli wrote a regular, non-empty file of the format (magic bytes); its size."""
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
        raise ValueError(f"kicad-cli pcb export {fmt} wrote no file")
    with path.open("rb") as fh:
        head = fh.read(len(_MAGIC[fmt]))
    if head != _MAGIC[fmt]:
        raise ValueError(f"kicad-cli pcb export {fmt} wrote no {fmt.upper()} file")
    return st.st_size
