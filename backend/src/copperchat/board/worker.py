"""One-process CPU worker for board computations (B-C4).

Jobs run one at a time in a lazily created, spawn-started single-process pool. A job's
`timeout_s` starts when it is submitted (time queued for the lock never counts); on timeout
or cancel of the running job the pool's own process is SIGKILLed and the pool replaced
before the lock is released, so the next job always gets a live pool.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import resource
import signal
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor
from multiprocessing import get_context
from typing import Any

from copperchat.errors import StructuredToolError

WORKER_MEMORY_LIMIT_BYTES = 3 * 1024**3
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


def shutdown_worker() -> None:
    """Stop the worker (app shutdown, test teardown); a later `run_cpu` starts a new one."""
    _kill_and_replace()


def _consume(fut: asyncio.Future[Any]) -> None:
    if not fut.cancelled():
        fut.exception()  # mark retrieved: a killed job's BrokenProcessPool is expected


def _timeout_error(what: str, why: str) -> StructuredToolError:
    return StructuredToolError(
        what,
        why,
        "Retry with a smaller board or region, or try again later.",
        code="board_compute_timeout",
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
            return af.result()
        except BaseException as exc:
            if type(exc).__name__ == "BrokenProcessPool":
                _kill_and_replace()
            raise
    finally:
        lock.release()
