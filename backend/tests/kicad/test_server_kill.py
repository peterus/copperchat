"""Review Focus 2: the real api-server dies mid-call."""

import asyncio
import os
import signal
import time
from typing import Any

import pytest

from copperchat.kicad_session import KicadSession, KicadUnavailable

pytestmark = pytest.mark.kicad


async def test_kill_mid_call_raises_unavailable_then_recovers(
    ecc83_session: KicadSession,
) -> None:
    proc = ecc83_session.process
    assert proc is not None and proc.pid
    pid = proc.pid
    started = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_reads(h: Any) -> int:
        loop.call_soon_threadsafe(started.set)
        n = 0
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            n += len(h.board().get_footprints())
        return n

    async def killer() -> None:
        await started.wait()
        await asyncio.sleep(0.5)
        os.kill(pid, signal.SIGKILL)  # only the api-server this test's session started

    with pytest.raises(KicadUnavailable):
        await asyncio.gather(ecc83_session.call(slow_reads, timeout=15), killer())
    assert ecc83_session.restart_count == 1
    assert ecc83_session.process is not None and ecc83_session.process.pid != pid
    count = await ecc83_session.call(lambda h: len(h.board().get_footprints()))
    assert count > 0
