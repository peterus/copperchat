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
def test_worker_rejects_bad_limits(argv: tuple[str, ...], tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        worker.parse_args(list(argv))
    # a valid SVG on stdin: a worker that accepted the argv would render it (exit 0)
    out = _run_worker(tmp_path, *argv)
    assert out.returncode == 2
    assert out.stdout == b""
    assert out.stderr == b"ValueError"


def test_worker_renders_with_good_limits(tmp_path: Path) -> None:
    """The control of the test above: the same stdin with valid argv renders a PNG."""
    out = _run_worker(tmp_path, "10", "10", "8", str(2**30))
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith(b"\x89PNG")


def _run_worker(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess[bytes]:
    svg = tmp_path / "in.svg"
    svg.write_bytes(SVG)
    with svg.open("rb") as stdin:
        return subprocess.run(
            [sys.executable, "-I", "-m", "copperchat.views._raster_worker", *argv],
            stdin=stdin,
            capture_output=True,
            timeout=60,
        )


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


async def test_board_cli_not_started_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing kicad-cli is reported as such, not as an unwritable board copy."""
    from types import SimpleNamespace

    from copperchat.views import board_render as br

    async def missing(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("kicad-cli")

    monkeypatch.setattr(br, "run_kicad_cli", missing)
    ctx: Any = SimpleNamespace(settings=None)
    with pytest.raises(StructuredToolError) as err:
        await br._cli(ctx, [], 1.0, Path("."), "pcb render")
    assert err.value.code == "render_failed"
    assert "kicad-cli could not be started (FileNotFoundError)" in err.value.why


def test_board_output_unreadable_is_named(tmp_path: Path) -> None:
    from copperchat.views import board_render as br

    out = tmp_path / "x.svg"
    out.mkdir()
    (out / "y").write_bytes(b"")
    with pytest.raises(StructuredToolError) as err:
        br._read_output(out / "y" / "z", 10, "SVG")  # ENOTDIR on lstat
    assert err.value.code == "render_failed"
    assert "kicad-cli's SVG could not be read (NotADirectoryError)" in err.value.why


async def test_worker_file_io_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The input write and the PNG read (up to 24 MiB / ~17 MB) run in a thread."""
    import threading

    threads: list[str] = []
    write_all, read_fd = fr._write_all, fr._read_fd

    def spy_write(fd: int, data: bytes) -> None:
        threads.append(threading.current_thread().name)
        write_all(fd, data)

    def spy_read(fd: int, size: int) -> bytes:
        threads.append(threading.current_thread().name)
        return read_fd(fd, size)

    monkeypatch.setattr(fr, "_write_all", spy_write)
    monkeypatch.setattr(fr, "_read_fd", spy_read)
    png, _ = await fr.rasterise(SVG, (4.0, 4.0))
    assert png.startswith(b"\x89PNG")
    assert len(threads) == 2
    assert threading.main_thread().name not in threads


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


@pytest.mark.parametrize("blocked", ["_write_all", "_read_fd"])
async def test_worker_cancel_in_thread_step(monkeypatch: pytest.MonkeyPatch, blocked: str) -> None:
    """Cancelled while the input write (setup) or the PNG read runs in its thread: the
    cancellation waits for the thread, then propagates; no fd stays open and the
    worker's temp home is removed (review round 2)."""
    import tempfile
    import threading

    entered, release = threading.Event(), threading.Event()
    real = getattr(fr, blocked)

    def blocking(*args: Any) -> Any:
        entered.set()
        assert release.wait(30)
        return real(*args)

    homes: list[str] = []
    mkdtemp = tempfile.mkdtemp

    def spy_mkdtemp(*args: Any, **kwargs: Any) -> str:
        homes.append(mkdtemp(*args, **kwargs))
        return homes[-1]

    monkeypatch.setattr(fr, blocked, blocking)
    monkeypatch.setattr(fr.tempfile, "mkdtemp", spy_mkdtemp)
    release.set()
    await fr.rasterise(SVG, (4.0, 4.0))  # warm-up: lazily opened loop / watcher fds
    release.clear()
    entered.clear()
    homes.clear()
    before = _open_fds()
    task = asyncio.create_task(fr.rasterise(SVG, (4.0, 4.0)))
    assert await asyncio.to_thread(entered.wait, 30)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()  # held until the thread is done with its fds
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _open_fds() == before
    assert len(homes) == 1 and not os.path.exists(homes[0])


class _Session:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def save_copy(self, kind: str, path: Path) -> None:
        if self.fail:
            raise PermissionError("read-only")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("(kicad_pcb)")


def _board_ctx(tmp_path: Path, session: _Session) -> Any:
    from types import SimpleNamespace

    project = SimpleNamespace(pcb_file=tmp_path / "b.kicad_pcb", dir=tmp_path, name="p")
    return SimpleNamespace(
        project=project,
        session=session,
        settings=None,
        editing=None,
        conversation_id=None,
        run_id=None,
    )


async def test_board_raster_oserror_not_blamed_on_board_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError of the raster step (here the rasteriser's mkdtemp) is reported as such,
    not as an unwritable board copy; a failing board copy still is (review round 2)."""
    import tempfile

    from copperchat.views import board_render as br
    from tests.unit.board.builders import board

    async def no_cli(*args: Any) -> None:
        return None

    def no_tmp(*args: Any, **kwargs: Any) -> str:
        raise PermissionError("no temp dir")

    monkeypatch.setattr(br, "_cli", no_cli)
    monkeypatch.setattr(br, "_prepare", lambda out, ov: (SVG, (4.0, 4.0)))
    monkeypatch.setattr(tempfile, "mkdtemp", no_tmp)
    with pytest.raises(StructuredToolError) as err:
        await br.render_board(_board_ctx(tmp_path, _Session()), "top", model=board())
    assert err.value.code == "render_failed"
    assert "board copy" not in err.value.why
    assert "temporary files could not be written (PermissionError)" in err.value.why

    with pytest.raises(StructuredToolError) as err:
        await br.render_board(_board_ctx(tmp_path, _Session(fail=True)), "top", model=board())
    assert "the board copy could not be written (PermissionError)" in err.value.why


async def test_board_hashes_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """render_board and pcb_status hash the board in a thread (review round 2)."""
    import threading
    from types import SimpleNamespace

    from copperchat.board import model as bm
    from copperchat.kicad_tools.board import status
    from copperchat.views import board_render as br
    from tests.unit.board.builders import board

    threads: list[str] = []

    def spy(real: Any) -> Any:
        def hashed(m: Any) -> str:
            threads.append(threading.current_thread().name)
            return str(real(m))

        return hashed

    for mod in (br, status):
        monkeypatch.setattr(mod, "placement_hash", spy(bm.placement_hash))
        monkeypatch.setattr(mod, "board_hash", spy(bm.board_hash))

    async def fake_2d(*args: Any) -> tuple[bytes, bytes]:
        return b"<svg/>", b"\x89PNG\r\n\x1a\n"

    monkeypatch.setattr(br, "_render_2d", fake_2d)
    m = board()
    out = await br.render_board(_board_ctx(tmp_path, _Session()), "top", model=m)
    assert (out.placement_hash, out.board_hash) == (bm.placement_hash(m), bm.board_hash(m))

    async def fake_model(ctx: Any) -> Any:
        return m

    async def fake_call(fn: Any, timeout: float) -> Any:
        return SimpleNamespace(unconnected=0)

    monkeypatch.setattr(status, "board_model", fake_model)
    ctx = _board_ctx(tmp_path, _Session())
    ctx.session.call = fake_call
    res = await status.pcb_status(ctx, {})
    assert res.data["board_hash"] == bm.board_hash(m)
    assert len(threads) == 4
    assert threading.main_thread().name not in threads
