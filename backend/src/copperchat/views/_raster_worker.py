"""Rasterising worker of the footprint render (spec §7.5): one SVG to one PNG with
CairoSVG, in a process of its own.

Run as `python -I -m copperchat.views._raster_worker <width_px> <height_px>` by
`footprint_render.rasterise`: the wrapper SVG on stdin, the PNG on stdout, exit 0; any
error exits non-zero with only the exception's class name on stderr. All three are
files in the per-render temp dir (opened by the parent), never pipes. CairoSVG's cost is
not bounded by any cheap estimate of the SVG (stroke width, self-crossing paths, ...), so
the parent kills this process after a wall-clock timeout; this process also caps its own
address space, CPU time and file size first (`resource.setrlimit`), so a runaway render
can neither exhaust the machine's memory or disk nor outlive a dead parent for long.
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


def limit_resources() -> None:
    _limit(resource.RLIMIT_AS, MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES)
    _limit(resource.RLIMIT_CPU, CPU_LIMIT_S, CPU_LIMIT_S + 2)
    _limit(resource.RLIMIT_FSIZE, FILE_LIMIT_BYTES, FILE_LIMIT_BYTES)


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
    limit_resources()
    try:
        width, height = (int(a) for a in argv)
        if not (0 < width <= MAX_SIDE_PX and 0 < height <= MAX_SIDE_PX):
            raise ValueError("size out of range")
        png = convert(sys.stdin.buffer.read(), width, height)
    except BaseException as exc:  # MemoryError, RecursionError, ...: class name only
        sys.stderr.write(type(exc).__name__[:80])
        return 2
    sys.stdout.buffer.write(png)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
