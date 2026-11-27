"""Heavy-process gate (SP4 round 2 R6, spec §22.2 decision 3): Freerouting, CPU jobs and
the parity DRC never all run at once; every wait is bounded."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from copperchat.board import _selftest, heavy
from copperchat.board.heavy import heavy_slot
from copperchat.board.worker import run_cpu, shutdown_worker
from copperchat.errors import StructuredToolError


@pytest.fixture(autouse=True)
async def _clean_worker() -> AsyncIterator[None]:
    yield
    shutdown_worker()


async def test_slot_serialises() -> None:
    order: list[str] = []

    async def job(name: str, hold: float) -> None:
        async with heavy_slot("freerouting", wait_s=5):
            order.append(f"{name} in")
            await asyncio.sleep(hold)
            order.append(f"{name} out")

    first = asyncio.create_task(job("a", 0.2))
    await asyncio.sleep(0.05)
    await asyncio.gather(first, job("b", 0))
    assert order == ["a in", "a out", "b in", "b out"]


def test_slot_per_loop() -> None:
    """One semaphore per event loop: two `asyncio.run` in a row both get a working slot
    (no "bound to a different event loop")."""

    async def contended() -> str | None:
        async def hold() -> None:
            async with heavy_slot("freerouting", wait_s=1):
                await asyncio.sleep(0.1)

        task = asyncio.create_task(hold())
        await asyncio.sleep(0.01)
        async with heavy_slot("parity", wait_s=2):
            seen = heavy.holder()
        await task
        return seen

    assert asyncio.run(contended()) == "parity"
    assert asyncio.run(contended()) == "parity"


async def test_gate_released_on_cancel() -> None:
    entered = asyncio.Event()

    async def hold() -> None:
        async with heavy_slot("freerouting", wait_s=1):
            entered.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(hold())
    await entered.wait()
    waiter = asyncio.create_task(heavy_slot("parity", wait_s=30).__aenter__())
    await asyncio.sleep(0.05)
    waiter.cancel()  # cancelled while waiting: must not take the slot
    with pytest.raises(asyncio.CancelledError):
        await waiter
    task.cancel()  # cancelled while holding
    with pytest.raises(asyncio.CancelledError):
        await task
    assert heavy.holder() is None
    async with heavy_slot("parity", wait_s=0.5):
        assert heavy.holder() == "parity"


async def test_gate_released_on_error() -> None:
    with pytest.raises(ValueError):
        async with heavy_slot("freerouting", wait_s=1):
            raise ValueError("boom")
    async with heavy_slot("parity", wait_s=0.1):
        pass


async def test_parity_waits_then_times_out() -> None:
    entered = asyncio.Event()

    async def hold() -> None:
        async with heavy_slot("freerouting", wait_s=1):
            entered.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(hold())
    await entered.wait()
    t0 = time.monotonic()
    with pytest.raises(StructuredToolError) as ei:
        async with heavy_slot("parity", wait_s=0.05):
            pytest.fail("entered a held slot")
    assert time.monotonic() - t0 < 1
    assert ei.value.code == "board_compute_timeout"
    assert ei.value.details == {"waiting_for": "freerouting"}
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_parity_waits_for_cpu_job() -> None:
    job = asyncio.create_task(run_cpu(_selftest.spin, 1.0, timeout_s=30))
    await asyncio.sleep(0.3)  # the job holds the worker lock (pool start included)
    with pytest.raises(StructuredToolError) as ei:
        async with heavy_slot("parity", wait_s=0.1):
            pytest.fail("entered while a CPU job runs")
    assert ei.value.details == {"waiting_for": "cpu_job"}
    assert heavy.holder() is None  # a failed parity entry releases the gate
    async with heavy_slot("parity", wait_s=30):
        assert job.done()  # entered only after the job
    await job


async def test_cpu_job_runs_during_parity() -> None:
    async with heavy_slot("parity", wait_s=5):
        assert heavy.holder() == "parity"
        assert await run_cpu(_selftest.square, 7, timeout_s=30, wait_s=5) == 49


async def test_freerouting_does_not_wait_for_cpu_job() -> None:
    job = asyncio.create_task(run_cpu(_selftest.spin, 1.0, timeout_s=30))
    await asyncio.sleep(0.3)
    async with heavy_slot("freerouting", wait_s=0.1):
        assert not job.done()
    await job
