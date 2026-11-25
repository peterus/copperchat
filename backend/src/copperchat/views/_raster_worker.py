"""Rasterising worker of the footprint render (spec §7.5): one SVG to one PNG with
CairoSVG, in a process of its own.

Run as `python -I -m copperchat.views._raster_worker <width_px> <height_px>
[<cpu_s> <memory_bytes>]` by `footprint_render.rasterise` (the two limits only for board
renders, B-C9; without them the constants below apply): the wrapper SVG on stdin, the
PNG on stdout, exit 0; any error exits non-zero with only the exception's class name on
stderr. All three are files in the per-render temp dir (opened by the parent), never
pipes. CairoSVG's cost is not bounded by any cheap estimate of the SVG (stroke width,
self-crossing paths, ...), so the parent kills this process after a wall-clock timeout;
this process also caps its own address space, CPU time and file size first
(`resource.setrlimit`), so a runaway render can neither exhaust the machine's memory or
disk nor outlive a dead parent for long.
"""

from __future__ import annotations

import resource
import sys

# Address space of the worker (CPython + cairo + one ARGB surface of at most
# 2000 x 2000 px, ~16 MB; the stock maximum renders well below 300 MB).
MEMORY_LIMIT_BYTES = 1536 * 1024 * 1024
# CPU seconds: a backstop above the parent's wall-clock timeout (`RASTER_TIMEOUT_S`),
# which is what normally ends a slow render. SIGXCPU at the soft, SIGKILL at the hard limit.
CPU_LIMIT_S = 30
MAX_SIDE_PX = 2000
# Largest file the worker may write (its stdout PNG, fontconfig's cache in HOME): the
# parent's PNG cap (`footprint_render.MAX_PNG_BYTES`, 2000 x 2000 RGBA + 1 MB) plus a
# margin, so the parent sees an oversized PNG and refuses it, but the disk never fills.
# Writing past it raises SIGXFSZ (the worker dies: a non-zero exit).
FILE_LIMIT_BYTES = 4 * MAX_SIDE_PX * MAX_SIDE_PX + 2 * 1024 * 1024


def _limit(which: int, value: int, hard: int) -> None:
    _soft, cur_hard = resource.getrlimit(which)
    if cur_hard != resource.RLIM_INFINITY:
        value, hard = min(value, cur_hard), min(hard, cur_hard)
    resource.setrlimit(which, (value, hard))


# Bounds of the optional argv limits (board renders): the parent's board raster timeout
# is at most 60 s and its memory cap at most 3 GiB (plan B-C9).
MAX_CPU_LIMIT_S = 120
MAX_MEMORY_LIMIT_BYTES = 3 * 1024 * 1024 * 1024


def limit_resources(cpu_s: int = CPU_LIMIT_S, memory_bytes: int = MEMORY_LIMIT_BYTES) -> None:
    """Address space `memory_bytes`, CPU `cpu_s` (hard + 2 s) and the fixed file-size
    cap; never above a hard limit this process inherited."""
    _limit(resource.RLIMIT_AS, memory_bytes, memory_bytes)
    _limit(resource.RLIMIT_CPU, cpu_s, cpu_s + 2)
    _limit(resource.RLIMIT_FSIZE, FILE_LIMIT_BYTES, FILE_LIMIT_BYTES)


def parse_args(argv: list[str]) -> tuple[int, int, int, int]:
    """(width, height, cpu_s, memory_bytes) from 2 or 4 integer argv items (2 → the
    constants); ValueError for anything else or a value out of range."""
    if len(argv) not in (2, 4):
        raise ValueError("expected 2 or 4 arguments")
    values = [int(a) for a in argv]
    width, height = values[:2]
    cpu_s, memory = values[2:] if len(values) == 4 else (CPU_LIMIT_S, MEMORY_LIMIT_BYTES)
    if not (0 < width <= MAX_SIDE_PX and 0 < height <= MAX_SIDE_PX):
        raise ValueError("size out of range")
    if not (0 < cpu_s <= MAX_CPU_LIMIT_S and 0 < memory <= MAX_MEMORY_LIMIT_BYTES):
        raise ValueError("limit out of range")
    return width, height, cpu_s, memory


def refuse(url: str, resource_type: str) -> bytes:
    """CairoSVG URL fetcher: the wrapper never needs an external resource."""
    raise ValueError(f"external resource refused: {url[:100]}")


def convert(svg: bytes, width: int, height: int) -> bytes:
    """PNG of `svg` at `width` x `height` px: CairoSVG with the refusing URL fetcher,
    never `unsafe=True`."""
    import cairosvg.surface  # type: ignore[import-untyped]  # after the limits are set

    png = cairosvg.surface.PNGSurface.convert(
        bytestring=svg,
        url_fetcher=refuse,
        unsafe=False,
        output_width=width,
        output_height=height,
    )
    if not isinstance(png, bytes):
        raise TypeError("CairoSVG returned no PNG")
    return png


def main(argv: list[str]) -> int:
    try:
        # the limits come from argv, so it is read first (a few ints; nothing else runs
        # before the limits are set)
        width, height, cpu_s, memory = parse_args(argv)
    except ValueError as exc:
        sys.stderr.write(type(exc).__name__)
        return 2
    limit_resources(cpu_s, memory)
    try:
        png = convert(sys.stdin.buffer.read(), width, height)
    except BaseException as exc:  # MemoryError, RecursionError, ...: class name only
        sys.stderr.write(type(exc).__name__[:80])
        return 2
    sys.stdout.buffer.write(png)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
