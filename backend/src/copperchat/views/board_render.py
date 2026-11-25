"""Board render pipeline (SP4 B-C9, spec §13): a copy of the live board
(`SaveCopyOfDocument`), `kicad-cli pcb export svg` of the view's layers, KiCad's SVG
through the footprint sanitiser with the board limits, zone polygons simplified, wrapped
(`board.render.wrap_svg`), the wrapper sanitised again and rasterised by the footprint
rasteriser's worker with the board CPU / memory limits; `3d` = `kicad-cli pcb render`.

Hardening, beyond the footprint pipeline's (`views.footprint_render`):
- every board kicad-cli call (here: the SVG export and the 3D render; later the parity
  DRC and the fab exports) runs under the process-wide `BOARD_CLI_LOCK` with an explicit
  timeout; a timeout is `render_failed` naming it. The board copy is taken before the
  lock (it holds the session's edit lock, never inside the CLI lock);
- the board copy and every kicad-cli output live in `renders/tmp/<uuid>.*` and are
  deleted on every way out (regular files only, never through a link);
- retention: board PNGs are kept (conversation images link them); board SVGs beyond the
  newest `KEEP_BOARD_SVGS` (and their `.svg.json` sidecars) are pruned at each render,
  from the list in `renders/board-svgs.json` only - footprint renders are never listed,
  so they are never touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import stat
import threading
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from copperchat.board.model import BoardModel, Rect, board_hash, placement_hash
from copperchat.board.render import (
    SVG_NS,
    Marker,
    block_hulls,
    highlight_shapes,
    simplify_paths,
    wrap_svg,
)
from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.board.read import board_model, require_board
from copperchat.kicad_tools.cli import run_kicad_cli
from copperchat.projects import agent_dir
from copperchat.tools.spec import ToolContext
from copperchat.views.footprint_render import (
    MAX_PNG_BYTES,
    SvgLimits,
    _settle,
    _tail,
    load_kicad_svg,
    rasterise,
)

log = logging.getLogger(__name__)

BoardView = Literal["top", "bottom", "layer", "placement", "3d"]
Angle = Literal["top", "bottom", "iso"]
BOARD_VIEWS: tuple[BoardView, ...] = ("top", "bottom", "layer", "placement", "3d")
ANGLES: tuple[Angle, ...] = ("top", "bottom", "iso")

# Process-wide: one board kicad-cli call at a time (like footprint_render._render_lock),
# each with its own explicit timeout.
BOARD_CLI_LOCK = asyncio.Semaphore(1)
BOARD_SVG_TIMEOUT_S = 60.0
BOARD_3D_TIMEOUT_S = 120.0
MiB = 1024 * 1024
# Rasterising caps of a board SVG (B-C9): at least 4x the largest value measured on the
# fixtures (P1c "As built": ecc83 and ecc83 with GND pours on F.Cu and B.Cu, largest
# raw export 77,674 bytes, 878 elements, 3,519 coordinate pairs, 1,834 in one element, a
# line span of 65.5 drawing extents). The demo board is small, so the B-C9 starting
# values stand (each far above 4x); the span cap is ~300x the measured value (headroom
# for denser boards; P9a re-measures on a large board).
BOARD_SVG_LIMITS = SvgLimits(
    max_bytes=24 * MiB,
    max_elements=200_000,
    max_coords=2_000_000,
    max_element_coords=50_000,
    max_span=20_000.0,
)
BOARD_MAX_SIDE_PX = 1600
BOARD_3D_SIZE = (1600, 1200)
# The rasteriser worker on the board path (measured on the same boards: at most 0.71 s
# and 74 MB peak RSS at 1600 px): wall clock at least 3x the measured time and at most
# 60 s - 30 s leaves room for boards far denser than the fixture; address space at least
# 2x the peak RSS and at most 3 GiB - an address-space limit counts every mapping
# (CPython, cairo, fonts), so it stays above the footprint worker's 1.5 GiB.
BOARD_RASTER_TIMEOUT_S = 30.0
BOARD_RASTER_MEMORY_BYTES = 2048 * MiB
SIMPLIFY_TOL_MM = 0.02
KEEP_BOARD_SVGS = 20
BOARD_SVGS_FILE = "board-svgs.json"
MAX_BOARD_SVGS_FILE_BYTES = 64 * 1024
# View layers (spec §13), KiCad plots them in this order (bottom → top).
VIEW_LAYERS: dict[str, tuple[str, ...]] = {
    "top": ("F.Cu", "F.Mask", "F.SilkS", "Edge.Cuts"),
    "bottom": ("B.Cu", "B.Mask", "B.SilkS", "Edge.Cuts"),
    "placement": ("F.CrtYd", "B.CrtYd", "F.Fab", "F.SilkS", "Edge.Cuts"),
}
_STEM = re.compile(r"^[0-9a-f]{32}$")
_list_lock = threading.Lock()  # read-modify-write of board-svgs.json


@dataclass(frozen=True)
class BoardRender:
    png: Path
    svg: Path | None  # None for 3d
    placement_hash: str
    board_hash: str


def _failed(
    why: str,
    suggestion: str = "Retry once; if it fails again use pcb_status or another view or region.",
) -> StructuredToolError:
    return StructuredToolError("Board render failed", why, suggestion, code="render_failed")


@contextlib.contextmanager
def _board_wording() -> Iterator[None]:
    """The footprint pipeline's `render_failed` errors (sanitiser, rasteriser) reworded
    for a board: same reason, board suggestion."""
    try:
        yield
    except StructuredToolError as exc:
        if exc.code == "render_failed" and exc.what != "Board render failed":
            raise _failed(exc.why) from None
        raise


# --- pure helpers -----------------------------------------------------------------------


def view_layers(view: BoardView, layer: str | None = None) -> list[str]:
    """kicad-cli's `--layers` for a 2D view; `layer` view = `<layer>,Edge.Cuts`."""
    if view == "layer":
        if not layer:
            raise ValueError("the layer view needs a layer")
        return [layer, "Edge.Cuts"]
    return list(VIEW_LAYERS[view])


def svg_export_args(view: BoardView, layer: str | None, board: Path, out: Path) -> list[str]:
    args = [
        "pcb",
        "export",
        "svg",
        "--mode-single",
        "--page-size-mode",
        "2",
        "--exclude-drawing-sheet",
        "--layers",
        ",".join(view_layers(view, layer)),
    ]
    if view == "bottom":
        args.append("--mirror")
    return [*args, "-o", str(out), str(board)]


def render_3d_args(angle: Angle, board: Path, out: Path) -> list[str]:
    side = ["--rotate", "-45,0,45", "--perspective"] if angle == "iso" else ["--side", angle]
    w, h = BOARD_3D_SIZE
    return [
        "pcb",
        "render",
        "-o",
        str(out),
        "--width",
        str(w),
        "--height",
        str(h),
        "--quality",
        "basic",
        *side,
        str(board),
    ]


# --- retention --------------------------------------------------------------------------


def _read_list(path: Path) -> list[str]:
    """The listed board render stems, oldest first; [] when missing, not a regular file,
    oversized or unreadable (logged: the next renders start a new list)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("board renders: %s unreadable (%s)", path.name, exc)
        return []
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BOARD_SVGS_FILE_BYTES:
        log.warning("board renders: %s ignored (not a small regular file)", path.name)
        return []
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("board renders: %s ignored (%s)", path.name, exc)
        return []
    if not isinstance(data, list):
        return []
    return [s for s in data if isinstance(s, str) and _STEM.fullmatch(s)]


def _write_list(path: Path, stems: list[str]) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(stems), "utf-8")
        os.replace(tmp, path)  # replaces a link itself, never its target
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _unlink_regular(path: Path) -> None:
    """Delete `path` if it is a regular file (lstat: a link is never followed); a missing
    file is skipped."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(st.st_mode):
        log.warning("board renders: %s not deleted (not a regular file)", path.name)
        return
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def record_board_svg(renders: Path, stem: str, *, keep: int = KEEP_BOARD_SVGS) -> list[str]:
    """Append `stem` to `renders/board-svgs.json` and prune: entries beyond the newest
    `keep` leave the list and their `<stem>.svg` and `<stem>.svg.json` are deleted.
    Returns the pruned stems. Blocking (run in a thread)."""
    if not _STEM.fullmatch(stem):
        raise ValueError(f"not a render stem: {stem[:40]!r}")
    path = renders / BOARD_SVGS_FILE
    with _list_lock:
        listed = [s for s in _read_list(path) if s != stem] + [stem]
        pruned, listed = listed[:-keep], listed[-keep:]
        _write_list(path, listed)
    for old in pruned:
        for name in (f"{old}.svg", f"{old}.svg.json"):
            _unlink_regular(renders / name)
    return pruned


def _remove_tmp(tmp: Path, stem: str) -> None:
    """The board copy and kicad-cli outputs of one render (`<stem>.*`; KiCad also copies
    the project's `.kicad_dru` next to the board)."""
    try:
        names = os.listdir(tmp)
    except FileNotFoundError:
        return
    for name in names:
        if name.startswith(f"{stem}."):
            _unlink_regular(tmp / name)


# --- pipeline ---------------------------------------------------------------------------


def _read_output(path: Path, max_bytes: int, what: str) -> bytes:
    """A kicad-cli output file: a non-empty regular file of at most `max_bytes` (C10: the
    exit code is no proof)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise _failed(f"kicad-cli wrote no {what}") from None
    except OSError as exc:
        raise _failed(f"kicad-cli's {what} could not be read ({type(exc).__name__})") from None
    if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
        raise _failed(f"kicad-cli wrote no usable {what}")
    if st.st_size > max_bytes:
        raise _failed(f"kicad-cli wrote more than {max_bytes} bytes of {what}")
    try:
        with path.open("rb") as fh:
            return fh.read(max_bytes + 1)
    except OSError as exc:
        raise _failed(f"kicad-cli's {what} could not be read ({type(exc).__name__})") from None


@dataclass(frozen=True)
class _Overlay:
    mirror: bool
    region: Rect | None
    model: BoardModel
    hulls: bool
    highlight_nets: tuple[str, ...]
    markers: tuple[Marker, ...]
    title: str


def _prepare(path: Path, ov: _Overlay) -> tuple[bytes, tuple[float, float]]:
    """Blocking, linear in the capped input: read KiCad's SVG, sanitise it with the board
    limits, simplify its zone polygons, wrap it, and sanitise the wrapper too (spec §13:
    the result goes through the same allow-list). Returns the SVG and its extent (mm)."""
    root = load_kicad_svg(
        _read_output(path, BOARD_SVG_LIMITS.max_bytes, "SVG"), limits=BOARD_SVG_LIMITS
    )
    simplify_paths(root, SIMPLIFY_TOL_MM)
    m = ov.model
    outline = m.outline
    wrapped = wrap_svg(
        ET.tostring(root, encoding="unicode"),
        extent=ov.region,
        mirror=ov.mirror,
        hulls=block_hulls(m) if ov.hulls else (),
        markers=ov.markers,
        highlight=highlight_shapes(m, ov.highlight_nets) if ov.highlight_nets else (),
        highlight_nets=ov.highlight_nets,
        title=ov.title,
        board_size=(outline.w, outline.h) if outline.kind != "none" else None,
    )
    final = load_kicad_svg(wrapped.encode("utf-8"), limits=BOARD_SVG_LIMITS)
    final.set("xmlns", SVG_NS)
    _, _, w, h = (float(v) for v in (final.get("viewBox") or "").split())
    # depth is bounded by the sanitiser's MAX_DEPTH, so the recursive serialiser is safe
    return ET.tostring(final, encoding="utf-8", xml_declaration=False), (w, h)


async def _cli(ctx: ToolContext, args: list[str], timeout_s: float, cwd: Path, what: str) -> None:
    """One board kicad-cli call (the caller holds `BOARD_CLI_LOCK`)."""
    try:
        result = await run_kicad_cli(ctx.settings, args, timeout=timeout_s, cwd=cwd)
    except TimeoutError:
        raise _failed(f"kicad-cli {what} took longer than {timeout_s:.0f} s") from None
    except OSError as exc:  # no kicad-cli binary, not executable, ...
        raise _failed(f"kicad-cli could not be started ({type(exc).__name__})") from None
    if result.returncode != 0:
        raise _failed(
            f"kicad-cli {what} exited with {result.returncode}: "
            f"{_tail(result.stderr or result.stdout)}"
        )


async def _render_2d(
    ctx: ToolContext, view: BoardView, layer: str | None, board: Path, ov: _Overlay
) -> tuple[bytes, bytes]:
    out = board.with_suffix(".svg")
    async with BOARD_CLI_LOCK:
        await _cli(
            ctx,
            svg_export_args(view, layer, board, out),
            BOARD_SVG_TIMEOUT_S,
            board.parent,
            "pcb export svg",
        )
        work = asyncio.ensure_future(asyncio.to_thread(_prepare, out, ov))
        try:
            svg, extent = await asyncio.shield(work)
        except asyncio.CancelledError:
            await _settle(work)  # hold the lock until the thread has ended
            raise
        # a process of its own, killed after the timeout and reaped (also on
        # cancellation) before the lock is released
        png, _ = await rasterise(
            svg,
            extent,
            max_side_px=BOARD_MAX_SIDE_PX,
            timeout_s=BOARD_RASTER_TIMEOUT_S,
            memory_bytes=BOARD_RASTER_MEMORY_BYTES,
        )
    return svg, png


async def _render_3d(ctx: ToolContext, angle: Angle, board: Path) -> bytes:
    out = board.with_suffix(".png")
    async with BOARD_CLI_LOCK:
        await _cli(
            ctx, render_3d_args(angle, board, out), BOARD_3D_TIMEOUT_S, board.parent, "pcb render"
        )
    png = await asyncio.to_thread(_read_output, out, MAX_PNG_BYTES, "PNG")
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise _failed("kicad-cli pcb render wrote no PNG")
    return png


async def render_board(
    ctx: ToolContext,
    view: BoardView,
    *,
    layer: str | None = None,
    region: Rect | None = None,
    highlight_nets: Sequence[str] = (),
    markers: Sequence[Marker] = (),
    angle: Angle = "top",
    model: BoardModel | None = None,
) -> BoardRender:
    """PNG (+ wrapper SVG for 2D views) of the live board under `.kicad-agent/renders/
    <uuid32>`; the hashes are those of the rendered state (`model`, read here when not
    given). `layer`, nets and region are validated by the caller (`pcb_render`)."""
    require_board(ctx)
    m = model if model is not None else await board_model(ctx)
    renders = agent_dir(ctx.project) / "renders"
    stem = uuid.uuid4().hex
    board = renders / "tmp" / f"{stem}.kicad_pcb"
    svg: bytes | None = None
    try:
        with _board_wording():
            await ctx.session.save_copy("pcb", board)
            if view == "3d":
                png = await _render_3d(ctx, angle, board)
            else:
                ov = _Overlay(
                    mirror=view == "bottom",
                    region=region,
                    model=m,
                    hulls=view == "placement",
                    highlight_nets=tuple(highlight_nets),
                    markers=tuple(markers),
                    title=f"{ctx.project.name} board, {view}" + (f" {layer}" if layer else ""),
                )
                svg, png = await _render_2d(ctx, view, layer, board, ov)
    except OSError as exc:  # the tmp dir or the board copy could not be written
        raise _failed(f"the board copy could not be written ({type(exc).__name__})") from None
    finally:
        await _settle(asyncio.ensure_future(asyncio.to_thread(_remove_tmp, board.parent, stem)))
    png_path = renders / f"{stem}.png"
    svg_path = renders / f"{stem}.svg" if svg is not None else None

    def write() -> None:
        try:
            png_path.write_bytes(png)
            if svg_path is not None and svg is not None:
                svg_path.write_bytes(svg)
        except OSError as exc:
            for path in (png_path, svg_path):  # never leave half a render behind
                if path is not None:
                    with contextlib.suppress(OSError):
                        path.unlink(missing_ok=True)
            raise _failed(
                f"the render could not be stored ({type(exc).__name__})",
                "Ask the user to check the project directory (disk space, permissions)",
            ) from None
        if svg_path is not None:
            try:
                record_board_svg(renders, stem)
            except OSError as exc:  # retention is housekeeping: the render stands
                log.warning("board renders: retention list not updated (%s)", exc)

    await asyncio.to_thread(write)
    return BoardRender(png_path, svg_path, placement_hash(m), board_hash(m))
