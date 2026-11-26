"""One-process CPU worker for board computations (B-C4).

Jobs run one at a time in a lazily created, spawn-started single-process pool. A job's
`timeout_s` starts when it is submitted (time queued for the lock never counts); on timeout
or cancel of the running job the pool's own process is SIGKILLed and the pool replaced
before the lock is released, so the next job always gets a live pool. A job that leaves the
worker above `WORKER_RECYCLE_MB` resident (Linux `/proc`) recycles it the same way, still
under the lock, so a queued job never starts on a worker that is about to be killed (R6).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import resource
import signal
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor
from multiprocessing import get_context
from typing import Any

from copperchat.errors import StructuredToolError

log = logging.getLogger(__name__)

WORKER_MEMORY_LIMIT_BYTES = 3 * 1024**3
WORKER_RECYCLE_MB = 512  # resident size after a job above which the worker is replaced
_JOB_MODULE_PREFIX = "copperchat.board."

_executor: ProcessPoolExecutor | None = None
_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None


def _limit_worker() -> None:
    """Pool initializer: cap the address space, never above the inherited hard limit."""
    _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    value = WORKER_MEMORY_LIMIT_BYTES
    new_hard = value
    if hard != resource.RLIM_INFINITY:
        value, new_hard = min(value, hard), min(new_hard, hard)
    resource.setrlimit(resource.RLIMIT_AS, (value, new_hard))


def _get_executor() -> ProcessPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ProcessPoolExecutor(
            max_workers=1, mp_context=get_context("spawn"), initializer=_limit_worker
        )
    return _executor


def _get_lock() -> asyncio.Lock:
    """The job lock; recreated when the running loop changed (e.g. one loop per test)."""
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock, _lock_loop = asyncio.Lock(), loop
    return _lock


def _kill_and_replace() -> None:
    """SIGKILL the executor's own worker process(es), shut it down; the next job re-creates."""
    global _executor
    ex, _executor = _executor, None
    if ex is None:
        return
    for pid in list(getattr(ex, "_processes", None) or {}):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    ex.shutdown(wait=False, cancel_futures=True)


def _worker_rss_mb() -> float | None:
    """The worker process's `VmRSS` in MB; None without a worker or `/proc` (non-Linux)."""
    ex = _executor
    for pid in list(getattr(ex, "_processes", None) or {}):
        try:
            with open(f"/proc/{pid}/status", encoding="ascii") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) / 1024
        except (OSError, ValueError):
            return None
    return None


def _recycle_if_large() -> None:
    rss = _worker_rss_mb()
    if rss is not None and rss > WORKER_RECYCLE_MB:
        log.info("board CPU worker recycled: %.0f MB resident > %d MB", rss, WORKER_RECYCLE_MB)
        _kill_and_replace()


async def wait_idle(timeout_s: float) -> bool:
    """True once no job runs or waits ahead of us (the job lock was free for a moment);
    False after `timeout_s`. Never keeps the lock (R6: a parity DRC waits for the running
    job, but does not block the next one)."""
    lock = _get_lock()
    try:
        async with asyncio.timeout(max(0.0, timeout_s)):
            await lock.acquire()
    except TimeoutError:
        return False
    lock.release()
    return True


def shutdown_worker() -> None:
    """Stop the worker (app shutdown, test teardown); a later `run_cpu` starts a new one."""
    _kill_and_replace()


def _consume(fut: asyncio.Future[Any]) -> None:
    if not fut.cancelled():
        fut.exception()  # mark retrieved: a killed job's BrokenProcessPool is expected


def _timeout_error(
    what: str, why: str, details: dict[str, Any] | None = None
) -> StructuredToolError:
    return StructuredToolError(
        what,
        why,
        "Retry with a smaller board or region, or try again later.",
        code="board_compute_timeout",
        details=details,
    )


async def run_cpu[T](
    fn: Callable[..., T],
    *args: Any,
    timeout_s: float,
    wait_s: float | None = None,
    **kwargs: Any,
) -> T:
    if not getattr(fn, "__module__", "").startswith(_JOB_MODULE_PREFIX):
        raise TypeError(f"{fn!r} is not a copperchat.board job function")
    lock = _get_lock()
    if wait_s is None:
        await lock.acquire()
    else:
        try:
            await asyncio.wait_for(lock.acquire(), wait_s)
        except TimeoutError:
            raise _timeout_error(
                "Board computation not started",
                f"Another board computation was still running after {wait_s:.1f}s.",
                {"waiting_for": "cpu_job"},  # the same shape as the heavy gate's (R6)
            ) from None
    try:
        cf: Future[T] = _get_executor().submit(functools.partial(fn, *args, **kwargs))
        af = asyncio.wrap_future(cf)
        af.add_done_callback(_consume)
        try:
            done, _ = await asyncio.wait({af}, timeout=timeout_s)
        except asyncio.CancelledError:
            _kill_and_replace()
            raise
        if not done:
            _kill_and_replace()
            raise _timeout_error(
                "Board computation timed out",
                f"The computation did not finish within {timeout_s:.1f}s.",
            )
        try:
            result = af.result()
        except BaseException as exc:
            if type(exc).__name__ == "BrokenProcessPool":
                _kill_and_replace()
            else:  # the job itself raised: the worker is alive, maybe big
                _recycle_if_large()
            raise
        _recycle_if_large()
        return result
    finally:
        lock.release()
