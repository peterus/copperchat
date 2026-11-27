"""Heavy-process gate (SP4 round 2 R6, spec §22.2 decision 3, owner D5 ≈ 5 GB).

The heavy processes of a board project — the Freerouting JVM, a CPU-worker job and the
`kicad-cli` parity DRC (≈ 2.6 GB) — must not all run at once. One `asyncio.Semaphore(1)`
per running event loop (a semaphore is bound to one loop; one loop per test) serialises
Freerouting runs and parity DRCs process-wide (across projects: memory is a host budget).
A parity DRC additionally waits until the CPU worker is idle, but never holds the worker
lock while it runs: a CPU job that starts during the DRC is not blocked (blocking it would
starve routing for up to `HEAVY_WAIT_S`); its own `run_cpu` timeout bounds it.

Lock order, everywhere: session apply lock → heavy gate → (CPU-worker lock, momentarily)
→ `BOARD_CLI_LOCK`. Every wait is bounded by `wait_s`; a timeout raises
`board_compute_timeout` with `details.waiting_for` naming what was still running.
"""

from __future__ import annotations

import asyncio
import time
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

from copperchat.board import worker
from copperchat.errors import StructuredToolError

HEAVY_WAIT_S = 120.0  # max wait for the gate (+ an idle CPU worker for a parity DRC)

HeavyKind = Literal["freerouting", "parity"]
WaitingFor = Literal["freerouting", "parity", "cpu_job", "kicad_cli"]


@dataclass
class _Gate:
    sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))
    holder: HeavyKind | None = None


_GATES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Gate] = weakref.WeakKeyDictionary()


def _gate() -> _Gate:
    loop = asyncio.get_running_loop()
    gate = _GATES.get(loop)
    if gate is None:
        gate = _GATES[loop] = _Gate()
    return gate


def holder() -> HeavyKind | None:
    """The kind holding the running loop's gate, None when free."""
    return _gate().holder


def heavy_timeout(waiting_for: WaitingFor, wait_s: float) -> StructuredToolError:
    return StructuredToolError(
        "Board computation not started",
        f"Another heavy board process ({waiting_for}) was still running after {wait_s:.1f}s.",
        "Try again when the running routing fill or check has finished.",
        code="board_compute_timeout",
        details={"waiting_for": waiting_for},
    )


@asynccontextmanager
async def heavy_slot(kind: HeavyKind, *, wait_s: float) -> AsyncIterator[None]:
    """Hold the heavy-process gate for the body. `"parity"` also waits (within the same
    `wait_s`) until the CPU worker is idle, without keeping its lock. Released on any exit,
    a cancellation included."""
    gate = _gate()
    wait_s = max(0.0, wait_s)
    deadline = time.monotonic() + wait_s
    try:
        async with asyncio.timeout(wait_s):
            await gate.sem.acquire()
    except TimeoutError:
        raise heavy_timeout(gate.holder or kind, wait_s) from None
    try:
        gate.holder = kind
        if kind == "parity" and not await worker.wait_idle(max(0.0, deadline - time.monotonic())):
            raise heavy_timeout("cpu_job", wait_s)
        yield
    finally:
        gate.holder = None
        gate.sem.release()


@asynccontextmanager
async def bounded(
    sem: asyncio.Semaphore, *, wait_s: float, waiting_for: WaitingFor
) -> AsyncIterator[None]:
    """`async with sem` whose acquisition is bounded by `wait_s` (the rest of a budget)."""
    wait_s = max(0.0, wait_s)
    try:
        async with asyncio.timeout(wait_s):
            await sem.acquire()
    except TimeoutError:
        raise heavy_timeout(waiting_for, wait_s) from None
    try:
        yield
    finally:
        sem.release()
