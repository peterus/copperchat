from __future__ import annotations

import asyncio
import os
import resource
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from copperchat.board import _selftest
from copperchat.board.worker import WORKER_MEMORY_LIMIT_BYTES, run_cpu, shutdown_worker
from copperchat.errors import StructuredToolError


@pytest.fixture(autouse=True)
async def _clean_worker() -> AsyncIterator[None]:
    yield
    shutdown_worker()


def _gone(pid: int, wait: float = 3.0) -> bool:
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if not Path(f"/proc/{pid}").exists():
            return True
        time.sleep(0.02)
    return False


async def test_runs_in_other_process() -> None:
    pid = await run_cpu(_selftest.getpid, timeout_s=30)
    assert pid != os.getpid()


async def test_timeout_kills_and_replaces() -> None:
    pid = await run_cpu(_selftest.getpid, timeout_s=30)
    t0 = time.monotonic()
    with pytest.raises(StructuredToolError) as ei:
        await run_cpu(_selftest.spin, 30, timeout_s=0.5)
    assert ei.value.code == "board_compute_timeout"
    assert time.monotonic() - t0 < 3
    assert _gone(pid)
    assert await run_cpu(_selftest.square, 3, timeout_s=30) == 9
    assert await run_cpu(_selftest.getpid, timeout_s=30) != pid


async def test_cancel_kills() -> None:
    pid = await run_cpu(_selftest.getpid, timeout_s=30)
    task = asyncio.create_task(run_cpu(_selftest.spin, 30, timeout_s=60))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _gone(pid)
    assert await run_cpu(_selftest.square, 2, timeout_s=30) == 4


async def test_rejects_foreign_function() -> None:
    with pytest.raises(TypeError):
        await run_cpu(os.getpid, timeout_s=5)


async def test_exception_propagates() -> None:
    with pytest.raises(ValueError, match="boom"):
        await run_cpu(_selftest.fail, "boom", timeout_s=30)


async def test_kwargs_passed_through() -> None:
    assert await run_cpu(_selftest.scaled, 3, timeout_s=30, factor=2) == 6


async def test_concurrent_calls_serialised() -> None:
    await run_cpu(_selftest.square, 0, timeout_s=30)
    t0 = time.monotonic()
    (a0, a1), (b0, b1) = await asyncio.gather(
        run_cpu(_selftest.spin, 0.5, timeout_s=2),
        run_cpu(_selftest.spin, 0.5, timeout_s=2),
    )
    assert time.monotonic() - t0 >= 1.0
    assert (b0 >= a1) or (a0 >= b1)


async def test_timeout_does_not_break_queued() -> None:
    await run_cpu(_selftest.square, 0, timeout_s=30)
    first = asyncio.create_task(run_cpu(_selftest.spin, 30, timeout_s=0.5))
    await asyncio.sleep(0.1)
    second = asyncio.create_task(run_cpu(_selftest.square, 4, timeout_s=30))
    with pytest.raises(StructuredToolError) as ei:
        await first
    assert ei.value.code == "board_compute_timeout"
    assert await second == 16


async def test_cancel_while_queued_keeps_running_job() -> None:
    await run_cpu(_selftest.square, 0, timeout_s=30)
    running = asyncio.create_task(run_cpu(_selftest.spin, 1.0, timeout_s=10))
    await asyncio.sleep(0.2)
    queued = asyncio.create_task(run_cpu(_selftest.square, 7, timeout_s=10))
    await asyncio.sleep(0.1)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    start, end = await running
    assert end - start >= 1.0
    assert await run_cpu(_selftest.square, 7, timeout_s=10) == 49


async def test_wait_s_bounds_queue_wait() -> None:
    await run_cpu(_selftest.square, 0, timeout_s=30)
    running = asyncio.create_task(run_cpu(_selftest.spin, 2, timeout_s=10))
    await asyncio.sleep(0.2)
    t0 = time.monotonic()
    with pytest.raises(StructuredToolError) as ei:
        await run_cpu(_selftest.square, 5, timeout_s=10, wait_s=0.3)
    assert ei.value.code == "board_compute_timeout"
    assert time.monotonic() - t0 < 1
    await running
    assert await run_cpu(_selftest.square, 5, timeout_s=10, wait_s=None) == 25


async def test_worker_rlimit_as() -> None:
    got = await run_cpu(_selftest.rlimit_as, timeout_s=30)
    hard = resource.getrlimit(resource.RLIMIT_AS)[1]
    expected = WORKER_MEMORY_LIMIT_BYTES
    if hard != resource.RLIM_INFINITY:
        expected = min(expected, hard)
    assert got == expected


async def test_peak_rss_measured() -> None:
    rss = await run_cpu(_selftest.peak_rss, timeout_s=30)
    print(f"worker peak RSS (selftest): {rss / 2**20:.1f} MiB")
    assert 0 < rss < WORKER_MEMORY_LIMIT_BYTES


# -- R6: recycling after large jobs -----------------------------------------------------------


async def test_recycle_above_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    from copperchat.board import worker

    pid = await run_cpu(_selftest.getpid, timeout_s=30)
    monkeypatch.setattr(worker, "_worker_rss_mb", lambda: worker.WORKER_RECYCLE_MB + 1.0)
    assert await run_cpu(_selftest.getpid, timeout_s=30) == pid  # the job ran, then recycled
    assert _gone(pid)
    monkeypatch.setattr(worker, "_worker_rss_mb", lambda: 10.0)
    assert await run_cpu(_selftest.getpid, timeout_s=30) != pid


async def test_no_recycle_below(monkeypatch: pytest.MonkeyPatch) -> None:
    from copperchat.board import worker

    monkeypatch.setattr(worker, "_worker_rss_mb", lambda: worker.WORKER_RECYCLE_MB - 1.0)
    pid = await run_cpu(_selftest.getpid, timeout_s=30)
    assert await run_cpu(_selftest.getpid, timeout_s=30) == pid


async def test_recycle_after_job_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from copperchat.board import worker

    pid = await run_cpu(_selftest.getpid, timeout_s=30)
    monkeypatch.setattr(worker, "_worker_rss_mb", lambda: worker.WORKER_RECYCLE_MB + 1.0)
    with pytest.raises(ValueError, match="big"):
        await run_cpu(_selftest.fail, "big", timeout_s=30)
    assert _gone(pid)


async def test_recycle_before_next_queued_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two jobs queued; the first leaves the worker big and recycles it while still holding
    the lock; the second starts only afterwards, in the new worker, and completes."""
    from copperchat.board import worker

    first_pid = await run_cpu(_selftest.getpid, timeout_s=30)
    calls = iter([worker.WORKER_RECYCLE_MB + 1.0])
    monkeypatch.setattr(worker, "_worker_rss_mb", lambda: next(calls, 10.0))
    a = asyncio.create_task(run_cpu(_selftest.getpid, timeout_s=30))
    b = asyncio.create_task(run_cpu(_selftest.getpid, timeout_s=30))
    pid_a, pid_b = await asyncio.gather(a, b)
    assert pid_a == first_pid
    assert pid_b != first_pid
    assert _gone(first_pid)


def test_worker_rss_reads_proc() -> None:
    """The real reader: None without a worker; the live worker's VmRSS (> 0 MB) with one."""
    from copperchat.board import worker

    shutdown_worker()
    assert worker._worker_rss_mb() is None

    async def go() -> float | None:
        await run_cpu(_selftest.getpid, timeout_s=30)
        return worker._worker_rss_mb()

    try:
        rss = asyncio.run(go())
    finally:
        shutdown_worker()
    assert rss is not None and 1.0 < rss < worker.WORKER_RECYCLE_MB


async def test_wait_idle() -> None:
    from copperchat.board import worker

    assert await worker.wait_idle(0.1) is True
    job = asyncio.create_task(run_cpu(_selftest.spin, 1.0, timeout_s=30))
    await asyncio.sleep(0.2)
    assert await worker.wait_idle(0.05) is False
    assert await worker.wait_idle(30) is True
    assert job.done()
    # never kept: the next job starts at once
    assert await run_cpu(_selftest.square, 5, timeout_s=30, wait_s=0.5) == 25
