import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from copperchat.config import Settings
from copperchat.kicad_session import (
    KicadCallTimeout,
    KicadSession,
    KicadSessionManager,
    KicadUnavailable,
)
from copperchat.projects import discover_projects
from tests.support.fake_kicad import FakeClientFactory, FakeLauncher, FakeServer


@pytest.fixture
def server() -> FakeServer:
    s = FakeServer()
    yield s
    s.release.set()


@pytest.fixture
async def session(
    make_settings: Callable[..., Settings], server: FakeServer
) -> AsyncIterator[KicadSession]:
    settings = make_settings(kicad_api_timeout_s=0.5)
    d: Path = settings.projects_root / "demo"
    d.mkdir(parents=True)
    (d / "demo.kicad_pro").touch()  # filename marker only
    manager = KicadSessionManager(
        settings, FakeLauncher(server), client_factory=FakeClientFactory(server)
    )
    s = await manager.open(discover_projects(settings.projects_root)[0])
    yield s
    await manager.close_all()


async def test_hang_times_out_restarts_and_next_call_works(
    session: KicadSession, server: FakeServer
) -> None:
    old_process = session.process
    with pytest.raises(KicadCallTimeout, match="restarted"):
        await session.call(lambda h: h.kicad.hang())
    # Unblock the abandoned worker thread now so the default executor does not
    # block loop teardown (the fixture's release.set() would come too late).
    server.release.set()
    assert session.restart_count == 1
    assert server.starts == 2
    assert old_process is not None and old_process.returncode is not None
    assert await session.call(lambda h: h.kicad.get_version().full_version) == "11.0.0-fake"


async def test_crash_raises_unavailable_then_recovers(
    session: KicadSession, server: FakeServer
) -> None:
    server.crash()
    with pytest.raises(KicadUnavailable) as info:
        await session.call(lambda h: h.kicad.ping())
    assert not isinstance(info.value, KicadCallTimeout)
    assert session.restart_count == 1
    await session.call(lambda h: h.kicad.ping())


async def test_call_is_never_retried_silently(session: KicadSession, server: FakeServer) -> None:
    calls = 0

    def flaky(h: object) -> None:
        nonlocal calls
        calls += 1
        # Runs in the worker thread: only flip the flag; never kill asyncio
        # subprocesses (crash()) from outside the event loop thread.
        server.dead = True
        h.kicad.ping()  # type: ignore[attr-defined]

    with pytest.raises(KicadUnavailable):
        await session.call(flaky)
    assert calls == 1


async def test_custom_timeout_overrides_default(session: KicadSession, server: FakeServer) -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(KicadCallTimeout):
        await session.call(lambda h: h.kicad.hang(), timeout=0.1)
    server.release.set()  # free the worker thread before teardown
    assert loop.time() - started < 5


async def test_failed_restart_reported_then_lazy_start_on_next_call(
    session: KicadSession, server: FakeServer
) -> None:
    server.fail_start_with_exit_code = 1
    server.crash()
    with pytest.raises(KicadUnavailable, match="could not be restarted"):
        await session.call(lambda h: h.kicad.ping())
    server.fail_start_with_exit_code = None
    await session.call(lambda h: h.kicad.ping())


async def test_concurrent_calls_are_serialised(session: KicadSession) -> None:
    order: list[int] = []

    def record(i: int) -> Callable[[object], None]:
        def _fn(_: object) -> None:
            order.append(i)

        return _fn

    await asyncio.gather(*(session.call(record(i)) for i in range(5)))
    assert sorted(order) == list(range(5)) and len(order) == 5


async def test_process_death_mid_call_is_detected_before_the_deadline(
    session: KicadSession, server: FakeServer
) -> None:
    # Spike 1C: a connected kipy client only notices a killed server after timeout_ms,
    # so the session watches the process itself.
    loop = asyncio.get_running_loop()
    loop.call_later(0.2, server.crash)
    started = loop.time()
    with pytest.raises(KicadUnavailable, match="died") as info:
        await session.call(lambda h: h.kicad.hang(), timeout=10)
    server.release.set()
    assert not isinstance(info.value, KicadCallTimeout)
    assert loop.time() - started < 5
    assert session.restart_count == 1
    await session.call(lambda h: h.kicad.ping())


async def test_dead_process_before_call_restarts_and_raises(
    session: KicadSession, server: FakeServer
) -> None:
    assert session.process is not None
    session.process.kill()
    await session.process.wait()
    with pytest.raises(KicadUnavailable, match="stopped"):
        await session.call(lambda h: h.kicad.ping())
    assert session.restart_count == 1
    await session.call(lambda h: h.kicad.ping())


async def test_cancelled_call_stops_the_server_and_next_call_restarts(
    session: KicadSession, server: FakeServer
) -> None:
    # An interrupted turn cancels its tool call; the abandoned worker thread must not
    # keep using the shared client, so the server is stopped and restarted lazily.
    old_process = session.process
    task = asyncio.create_task(session.call(lambda h: h.kicad.hang(), timeout=10))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    server.release.set()
    assert old_process is not None and old_process.returncode is not None
    assert session.process is None
    assert await session.call(lambda h: h.kicad.get_version().full_version) == "11.0.0-fake"
    assert server.starts == 2
