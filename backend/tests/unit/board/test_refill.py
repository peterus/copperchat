"""`api.refill` busy probing and the session's busy slot (B-C3), against fakes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from kipy.errors import ApiError
from kipy.errors import ConnectionError as KipyConnectionError
from kipy.proto.common import ApiStatusCode

from copperchat.config import Settings
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle, KicadSession, KicadSessionManager
from copperchat.kicad_session import session as session_mod
from copperchat.kicad_tools.board import api
from copperchat.kicad_tools.board.api import (
    REFILL_BUSY_GRACE_S,
    RefillTimeout,
    refill,
    refill_call_timeout,
)
from copperchat.projects import discover_projects
from tests.support.fake_kicad import FakeClientFactory, FakeLauncher, FakeServer


def _busy() -> ApiError:
    return ApiError("KiCad is busy", code=ApiStatusCode.AS_BUSY)


class FakeBoard:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.refills: list[dict[str, Any]] = []

    def refill_zones(self, **kwargs: Any) -> None:
        self.log.append("RefillZones")
        self.refills.append(kwargs)


class FakeKicad:
    """`ping` follows `script`: an exception instance is raised, None returns."""

    def __init__(self, script: Iterator[BaseException | None]) -> None:
        self.log: list[str] = []
        self.script = script
        self.board = FakeBoard(self.log)

    def get_board(self) -> FakeBoard:
        return self.board

    def ping(self) -> None:
        self.log.append("Ping")
        outcome = next(self.script)
        if outcome is not None:
            raise outcome


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(api, "monotonic", c.monotonic)
    monkeypatch.setattr(api, "sleep", c.sleep)
    return c


def _handle(kicad: Any, generation: int = 1) -> KicadHandle:
    return KicadHandle(kicad, frozenset({"pcb"}), generation=generation)


def test_refill_waits_until_idle(clock: Clock) -> None:
    kicad = FakeKicad(iter([_busy(), _busy(), _busy(), None]))
    refill(_handle(kicad), timeout_s=12.5)
    assert kicad.log == ["RefillZones", "Ping", "Ping", "Ping", "Ping"]
    assert kicad.board.refills == [{"block": True, "max_poll_seconds": 12.5}]


def test_refill_connection_error_is_busy(clock: Clock) -> None:
    kicad = FakeKicad(iter([KipyConnectionError("timeout"), None]))
    refill(_handle(kicad), timeout_s=5)
    assert kicad.log == ["RefillZones", "Ping", "Ping"]


def test_refill_other_api_error_raises(clock: Clock) -> None:
    kicad = FakeKicad(iter([ApiError("boom", code=ApiStatusCode.AS_BAD_REQUEST)]))
    with pytest.raises(ApiError, match="boom"):
        refill(_handle(kicad), timeout_s=5)


def test_refill_timeout_sets_busy_slot(clock: Clock) -> None:
    kicad = FakeKicad(iter(lambda: _busy(), None))  # type: ignore[arg-type, return-value]
    h = _handle(kicad, generation=7)
    with pytest.raises(RefillTimeout) as info:
        refill(h, timeout_s=3)
    err = info.value
    assert isinstance(err, StructuredToolError)
    assert err.code == "refill_timeout"
    assert err.details == {"timeout_s": 3, "grace_s": REFILL_BUSY_GRACE_S}
    assert h.busy_slot.get() == 7
    assert set(kicad.log[1:]) == {"Ping"}  # nothing but Ping while busy
    assert clock.now - 1000.0 >= REFILL_BUSY_GRACE_S


def test_refill_call_timeout() -> None:
    assert refill_call_timeout(60) == 105
    assert api.drc_call_timeout(60) == 105


# -- the session side: busy slot + ping-until-idle before the next callable ---------------


@pytest.fixture
async def session(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[KicadSession]:
    monkeypatch.setattr(session_mod, "BUSY_POLL_S", 0.0)
    settings = make_settings()
    d: Path = settings.projects_root / "demo"
    d.mkdir(parents=True)
    for suffix in ("kicad_pro", "kicad_sch", "kicad_pcb"):  # filename markers only
        (d / f"demo.{suffix}").touch()
    server = FakeServer()
    manager = KicadSessionManager(
        settings, FakeLauncher(server), client_factory=FakeClientFactory(server)
    )
    s = await manager.open(discover_projects(settings.projects_root)[0])
    yield s
    server.release.set()
    await manager.close_all()


def _script_ping(s: KicadSession, script: Iterator[BaseException | None], log: list[str]) -> None:
    def ping() -> None:
        log.append("Ping")
        outcome = next(script)
        if outcome is not None:
            raise outcome

    s._kicad.ping = ping  # instance attribute on the fake client


async def test_refill_timeout_after_grace(session: KicadSession, clock: Clock) -> None:
    log: list[str] = []
    board = FakeBoard(log)
    session._kicad.get_board = lambda: board
    _script_ping(session, iter(lambda: _busy(), None), log)  # type: ignore[arg-type, return-value]
    with pytest.raises(RefillTimeout):
        await session.call(lambda h: refill(h, timeout_s=0.01), timeout=refill_call_timeout(0.01))
    assert session.refill_busy == session.generation

    # The next call pings until idle before its callable runs, and clears the slot.
    log.clear()
    _script_ping(session, iter([_busy(), KipyConnectionError("t"), None]), log)
    assert await session.call(lambda h: log.append("fn") or "done") == "done"
    assert log == ["Ping", "Ping", "Ping", "fn"]
    assert session.refill_busy is None

    log.clear()  # idle now: no probing any more
    await session.call(lambda h: log.append("fn"))
    assert log == ["fn"]


async def test_busy_slot_of_older_generation_is_ignored(session: KicadSession) -> None:
    log: list[str] = []
    _script_ping(session, iter([]), log)
    session.refill_busy = session.generation - 1
    await session.call(lambda h: log.append("fn"))
    assert log == ["fn"]


async def test_busy_wait_other_api_error_raises(session: KicadSession) -> None:
    log: list[str] = []
    _script_ping(session, iter([ApiError("nope", code=ApiStatusCode.AS_BAD_REQUEST)]), log)
    session.refill_busy = session.generation
    with pytest.raises(ApiError, match="nope"):
        await session.call(lambda h: log.append("fn"))
    assert log == ["Ping"]


async def test_slots_reach_the_handle_and_restart_clears_them(session: KicadSession) -> None:
    def write(h: KicadHandle) -> tuple[int, Any, Any]:
        h.drc_slot.set((h.generation, "tok"))
        return h.generation, h.drc_slot.get(), h.busy_slot.get()

    gen, drc, busy = await session.call(write)
    assert gen == session.generation
    assert drc == (gen, "tok") and busy is None
    assert session.pending_drc == (gen, "tok")
    session.refill_busy = 99
    await session.restart()
    assert session.pending_drc is None and session.refill_busy is None


def test_handle_without_slots_gets_private_ones() -> None:
    h = KicadHandle(object())
    assert h.generation == 0 and h.drc_slot.get() is None and h.busy_slot.get() is None
    h.drc_slot.set((0, "t"))
    assert h.drc_slot.get() == (0, "t")
    assert KicadHandle(object()).drc_slot.get() is None
