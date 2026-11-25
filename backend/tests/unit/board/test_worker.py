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
