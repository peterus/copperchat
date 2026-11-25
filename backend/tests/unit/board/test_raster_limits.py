"""The rasteriser's CPU and memory limits on the board path, the unchanged footprint argv,
the worker's 2-or-4 argv and the board SVG retention (SP4 P1c Task 5, B-C9)."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from copperchat.errors import StructuredToolError
from copperchat.views import _raster_worker as worker
from copperchat.views import footprint_render as fr
from copperchat.views.board_render import BOARD_SVGS_FILE, KEEP_BOARD_SVGS, record_board_svg

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 4 4"><rect width="2" height="2"/></svg>'
)


class _Spy:
    def __init__(self) -> None:
        self.args: tuple[Any, ...] = ()

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.args = args
        raise OSError("not started in this test")


async def _argv(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> tuple[Any, ...]:
    spy = _Spy()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    with pytest.raises(StructuredToolError):
        await fr.rasterise(SVG, (4.0, 4.0), **kwargs)
    return spy.args


async def test_rasterise_passes_cpu_and_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    args = await _argv(monkeypatch, max_side_px=1600, timeout_s=7.2, memory_bytes=2**30)
    assert args[: len(fr.RASTER_WORKER)] == fr.RASTER_WORKER
    assert args[-2:] == ("8", str(2**30))
    assert len(args) == len(fr.RASTER_WORKER) + 4


async def test_footprint_argv_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    w, h, _ = fr._raster_size((4.0, 4.0))
    args = await _argv(monkeypatch)
    assert args == (*fr.RASTER_WORKER, str(w), str(h))


def _limits_in_child(*argv: str) -> tuple[tuple[int, int], tuple[int, int]]:
    """RLIMIT_CPU and RLIMIT_AS of a fresh interpreter after the worker's own parsing
    and `limit_resources` (a process of its own: the limits cannot be lowered back)."""
    code = (
        "import resource, sys\n"
        "from copperchat.views import _raster_worker as w\n"
        "_, _, cpu, mem = w.parse_args(sys.argv[1:])\n"
        "w.limit_resources(cpu, mem)\n"
        "print((resource.getrlimit(resource.RLIMIT_CPU), "
        "resource.getrlimit(resource.RLIMIT_AS)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code, *argv], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    cpu, mem = ast.literal_eval(out.stdout.strip())
    return tuple(cpu), tuple(mem)


def test_worker_two_args_uses_defaults() -> None:
    cpu, mem = _limits_in_child("10", "10")
    assert cpu == (worker.CPU_LIMIT_S, worker.CPU_LIMIT_S + 2)
    assert mem == (worker.MEMORY_LIMIT_BYTES, worker.MEMORY_LIMIT_BYTES)


def test_worker_four_args_sets_limits() -> None:
    cpu, mem = _limits_in_child("10", "10", "8", str(2**30))
    assert cpu == (8, 10)
    assert mem == (2**30, 2**30)


@pytest.mark.parametrize(
    "argv",
    [
        ("10", "10", "x", "100"),
        ("10", "10", "8", "1.5"),
        ("10", "10", "0", "100"),
        ("10", "10", "8", "-1"),
        ("10", "10", "8"),
        ("10", "10", "8", str(4 * 2**30)),
    ],
)
def test_worker_rejects_bad_limits(argv: tuple[str, ...]) -> None:
    out = subprocess.run(
        [sys.executable, "-I", "-m", "copperchat.views._raster_worker", *argv],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
    )
    assert out.returncode != 0
    assert out.stdout == b""


def test_board_svg_retention(tmp_path: Path) -> None:
    renders = tmp_path / "renders"
    renders.mkdir()
    stems = [uuid.uuid4().hex for _ in range(25)]
    for stem in stems:
        (renders / f"{stem}.png").write_bytes(b"png")
        (renders / f"{stem}.svg").write_bytes(b"svg")
        (renders / f"{stem}.svg.json").write_text("{}")
        record_board_svg(renders, stem)
    footprint = uuid.uuid4().hex  # an fp_render: never listed
    (renders / f"{footprint}.svg").write_bytes(b"svg")
    (renders / f"{footprint}.svg.json").write_text("{}")
    record_board_svg(renders, stems[-1])  # re-recording one is harmless

    kept, gone = stems[-KEEP_BOARD_SVGS:], stems[:-KEEP_BOARD_SVGS]
    assert len(gone) == 5
    for stem in kept:
        assert (renders / f"{stem}.svg").is_file()
        assert (renders / f"{stem}.svg.json").is_file()
    for stem in gone:
        assert not (renders / f"{stem}.svg").exists()
        assert not (renders / f"{stem}.svg.json").exists()
    for stem in stems:
        assert (renders / f"{stem}.png").is_file()
    assert (renders / f"{footprint}.svg").is_file()
    assert (renders / f"{footprint}.svg.json").is_file()
    assert json.loads((renders / BOARD_SVGS_FILE).read_text()) == kept


def test_board_svg_retention_skips_links_and_bad_entries(tmp_path: Path) -> None:
    renders = tmp_path / "renders"
    renders.mkdir()
    victim = tmp_path / "victim.svg"
    victim.write_bytes(b"keep me")
    linked = uuid.uuid4().hex
    os.symlink(victim, renders / f"{linked}.svg")
    (renders / BOARD_SVGS_FILE).write_text(json.dumps(["../escape", linked]))
    for _ in range(KEEP_BOARD_SVGS):
        record_board_svg(renders, uuid.uuid4().hex)  # missing files are skipped
    assert victim.read_bytes() == b"keep me"
    assert (renders / f"{linked}.svg").is_symlink()
    assert len(json.loads((renders / BOARD_SVGS_FILE).read_text())) == KEEP_BOARD_SVGS
