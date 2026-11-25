"""Tiny module-level job functions for the CPU worker's tests (and P5a's perf test)."""

from __future__ import annotations

import os
import resource
import time


def square(x: int) -> int:
    return x * x


def scaled(x: int, *, factor: int) -> int:
    return x * factor


def spin(seconds: float) -> tuple[float, float]:
    """Busy-wait `seconds`; the `time.monotonic()` start and end stamps."""
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        pass
    return start, time.monotonic()


def getpid() -> int:
    return os.getpid()


def rlimit_as() -> int:
    return resource.getrlimit(resource.RLIMIT_AS)[0]


def peak_rss() -> int:
    """The process's own peak resident set (`VmHWM`) in bytes."""
    with open("/proc/self/status", encoding="ascii") as f:
        for line in f:
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    raise RuntimeError("VmHWM not found")


def fail(message: str) -> None:
    raise ValueError(message)
