"""`api.refill` busy probing and the session's busy slot (B-C3), against fakes.

kipy's real `Board.refill_zones` runs against a fake protobuf client; kipy's, the api
module's and the session's clocks are one fake clock, so a Ping that "hangs" for the
transport timeout simply advances it."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import kipy.board
import pytest
from google.protobuf.empty_pb2 import Empty
from kipy.errors import ApiError
from kipy.errors import ConnectionError as KipyConnectionError
from kipy.proto.board import board_commands_pb2
from kipy.proto.common import ApiStatusCode
from kipy.proto.common.commands import Ping
from kipy.proto.common.types import base_types_pb2

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

HANG = object()  # a Ping that blocks for the whole transport timeout, then fails


def _busy() -> ApiError:
    return ApiError("KiCad is busy", code=ApiStatusCode.AS_BUSY)


def _always(outcome: Any) -> Iterator[Any]:
    while True:
        yield outcome() if callable(outcome) else outcome


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
    for mod in (api, kipy.board):
        monkeypatch.setattr(mod, "monotonic", c.monotonic)
        monkeypatch.setattr(mod, "sleep", c.sleep)
    monkeypatch.setattr(session_mod, "_clock", c.monotonic)
    monkeypatch.setattr(session_mod, "_sleep", c.sleep)
    return c


class FakeConn:
    """A pynng `Req0` stand-in: the timeouts kipy and `_short_timeout` set (ms)."""

    def __init__(self, timeout_ms: int) -> None:
        self.recv_timeout = self.send_timeout = timeout_ms
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeClient:
    """A kipy `KiCadClient` (same private parts: `_timeout_ms`, `_connected`, `_conn`,
    `_connect`): RefillZones is answered at once, each Ping follows `script` (None =
    answered, an exception = raised, HANG = the clock advances by the socket's receive
    timeout, the client disconnects and raises a transport error - as kipy does)."""

    def __init__(self, clock: Clock, script: Iterator[Any], timeout_ms: int) -> None:
        self.clock = clock
        self.script = script
        self._timeout_ms = timeout_ms
        self.conns: list[FakeConn] = []
        self._connected = False
        self._connect()
        self.log: list[str] = []
        self.ping_timeouts: list[int] = []

    def _connect(self) -> None:
        self._conn = FakeConn(self._timeout_ms)
        self.conns.append(self._conn)
        self._connected = True

    def send(self, command: Any, response_type: Any) -> Any:
        if not self._connected:
            self._connect()
        self.log.append(type(command).__name__)
        if isinstance(command, Ping):
            self.ping_timeouts.append(self._conn.recv_timeout)
            outcome = next(self.script)
            if outcome is HANG:
                self.clock.now += self._conn.recv_timeout / 1000
                self._connected = False
                raise KipyConnectionError("Error receiving reply from KiCad: timed out")
            if outcome is not None:
                raise outcome
        else:
            assert isinstance(command, board_commands_pb2.RefillZones)
        return Empty()


class FakeBoard:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.document = base_types_pb2.DocumentSpecifier()


class FakeKicad:
    def __init__(self, client: FakeClient) -> None:
        self._client = client  # where `transport_timeout_s` reads `_timeout_ms`

    def get_board(self) -> FakeBoard:
        return FakeBoard(self._client)

    def ping(self) -> None:
        self._client.send(Ping(), Empty)


def _kicad(clock: Clock, script: Iterator[Any], timeout_ms: int = 2000) -> FakeKicad:
    return FakeKicad(FakeClient(clock, script, timeout_ms))


def _handle(kicad: Any, generation: int = 1) -> KicadHandle:
    return KicadHandle(kicad, frozenset({"pcb"}), generation=generation)


def test_refill_waits_until_idle(clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    polls: list[float] = []

    class SpyBoard(kipy.board.Board):
        def refill_zones(self, *args: Any, **kwargs: Any) -> None:
            polls.append(kwargs["max_poll_seconds"])
            assert kwargs["block"] is True
            super().refill_zones(*args, **kwargs)

    monkeypatch.setattr(api, "Board", SpyBoard)
    kicad = _kicad(clock, iter([_busy(), _busy(), _busy(), None]))
    h = _handle(kicad)
    h.busy_slot.set(1)
    refill(h, timeout_s=12.5)
    assert kicad._client.log == ["RefillZones", "Ping", "Ping", "Ping", "Ping"]
    assert polls == [12.5]
    assert h.busy_slot.get() is None


def test_refill_short_budget_idle_needs_no_extra_ping(clock: Clock) -> None:
    """kipy's own Ping (after 0.5 s) was answered: idle, even past a 0.01 s budget."""
    kicad = _kicad(clock, iter([None]), timeout_ms=30_000)
    refill(_handle(kicad), timeout_s=0.01)
    assert kicad._client.log == ["RefillZones", "Ping"]


def test_refill_connection_error_is_busy(clock: Clock) -> None:
    kicad = _kicad(clock, iter([KipyConnectionError("timeout"), None]))
    refill(_handle(kicad), timeout_s=5)
    assert kicad._client.log == ["RefillZones", "Ping", "Ping"]


def test_refill_other_api_error_raises(clock: Clock) -> None:
    kicad = _kicad(clock, iter([ApiError("boom", code=ApiStatusCode.AS_BAD_REQUEST)]))
    with pytest.raises(ApiError, match="boom"):
        refill(_handle(kicad), timeout_s=5)


def test_refill_timeout_sets_busy_slot(clock: Clock) -> None:
    kicad = _kicad(clock, _always(_busy))
    h = _handle(kicad, generation=7)
    start = clock.now
    with pytest.raises(RefillTimeout) as info:
        refill(h, timeout_s=3)
    err = info.value
    assert isinstance(err, StructuredToolError)
    assert err.code == "refill_timeout"
    assert err.details == {"timeout_s": 3, "grace_s": REFILL_BUSY_GRACE_S}
    assert h.busy_slot.get() == 7
    assert kicad._client.log[0] == "RefillZones"
    assert set(kicad._client.log[1:]) == {"Ping"}  # nothing but Ping while busy
    # the grace is used (pings of ≤ 2 s fit until D - 2 s), but never past D
    assert 3 + REFILL_BUSY_GRACE_S - 2.5 <= clock.now - start <= 3 + REFILL_BUSY_GRACE_S


def _sockets_clean(client: FakeClient) -> None:
    """Every socket is back at the normal timeouts or closed (no short timeout leaks)."""
    for conn in client.conns:
        assert conn.closed or (conn.recv_timeout, conn.send_timeout) == (30_000, 30_000)


@pytest.mark.parametrize("timeout_s", [0.01, 20.0, 60.0])
def test_refill_hanging_pings_stay_inside_the_call_budget(clock: Clock, timeout_s: float) -> None:
    """Every Ping blocks for its socket's receive timeout: kipy's own Pings (normal
    30 s) overrun its deadline by one, the 2 s probes are only sent while they end before
    start + t + 30, so `refill` ends within t + 30.5 s - inside `refill_call_timeout(t)`
    = t + 45 s."""
    kicad = _kicad(clock, _always(HANG), timeout_ms=30_000)
    h = _handle(kicad, generation=3)
    start = clock.now
    with pytest.raises(RefillTimeout):
        refill(h, timeout_s=timeout_s)
    elapsed = clock.now - start
    assert elapsed <= timeout_s + 30.5 < refill_call_timeout(timeout_s)
    assert h.busy_slot.get() == 3
    _sockets_clean(kicad._client)


def test_refill_probes_use_the_short_timeout(clock: Clock) -> None:
    """kipy's Pings get AS_BUSY at once; afterwards the probes (2 s each) hang - the
    grace is spent on them up to D, never past it."""
    script = iter([_busy()] * 120 + [HANG] * 100)  # 60 s of kipy polls, then hangs
    kicad = _kicad(clock, script, timeout_ms=30_000)
    start = clock.now
    with pytest.raises(RefillTimeout):
        refill(_handle(kicad), timeout_s=60)
    timeouts = kicad._client.ping_timeouts
    probes = timeouts[120:]
    assert set(timeouts[:120]) == {30_000} and probes and set(probes) == {2_000}
    assert 60 + REFILL_BUSY_GRACE_S - 2.5 <= clock.now - start <= 60 + REFILL_BUSY_GRACE_S
    _sockets_clean(kicad._client)


def test_short_timeout_restored_after_a_probe(clock: Clock) -> None:
    kicad = _kicad(clock, iter([None, None]), timeout_ms=30_000)
    assert session_mod.probe_timeout_s(kicad) == session_mod.PROBE_PING_TIMEOUT_S
    assert session_mod.kicad_busy(kicad) is False
    assert kicad._client.ping_timeouts == [2_000]
    assert len(kicad._client.conns) == 1
    _sockets_clean(kicad._client)
    kicad.ping()  # a normal request afterwards: normal timeout, same socket
    assert kicad._client.ping_timeouts == [2_000, 30_000]


def test_timed_out_probe_closes_its_socket(clock: Clock) -> None:
    kicad = _kicad(clock, iter([HANG, None]), timeout_ms=30_000)
    start = clock.now
    assert session_mod.kicad_busy(kicad) is True
    assert clock.now - start == pytest.approx(2.0)
    first = kicad._client.conns[0]
    assert first.closed
    kicad.ping()  # kipy reconnects with its normal timeout
    assert kicad._client.ping_timeouts[-1] == 30_000
    _sockets_clean(kicad._client)


def test_probe_without_private_parts_uses_normal_timeout() -> None:
    class Plain:
        def ping(self) -> None:
            pass

    assert session_mod.probe_timeout_s(Plain()) == session_mod.DEFAULT_TRANSPORT_TIMEOUT_S
    assert session_mod.kicad_busy(Plain()) is False


def test_refill_call_timeout() -> None:
    assert refill_call_timeout(60) == 105
    assert api.drc_call_timeout(60) == 105


def test_transport_timeout_from_client() -> None:
    assert session_mod.transport_timeout_s(_kicad(Clock(), iter([]), 12_000)) == 12.0
    assert session_mod.transport_timeout_s(object()) == session_mod.DEFAULT_TRANSPORT_TIMEOUT_S


# -- the session side: busy slot + bounded ping-until-idle before the next callable --------


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


def _script_ping(
    s: KicadSession, script: Iterator[Any], log: list[str], clock: Clock | None = None
) -> None:
    """The session's fake client (no `_client`: transport timeout = the 30 s default)."""

    def ping() -> None:
        log.append("Ping")
        outcome = next(script)
        if outcome is HANG:
            assert clock is not None
            clock.now += session_mod.DEFAULT_TRANSPORT_TIMEOUT_S
            raise KipyConnectionError("timed out")
        if outcome is not None:
            raise outcome

    s._kicad.ping = ping  # instance attribute on the fake client


async def test_refill_timeout_then_next_call_waits(session: KicadSession, clock: Clock) -> None:
    client = FakeClient(clock, _always(_busy), timeout_ms=30_000)
    session._kicad.get_board = lambda: FakeBoard(client)
    log: list[str] = []
    _script_ping(session, _always(_busy), log)
    with pytest.raises(RefillTimeout):
        await session.call(lambda h: refill(h, timeout_s=0.01), timeout=refill_call_timeout(0.01))
    assert session.refill_busy == session.generation

    # The next call (with room for a 30 s Ping) pings until idle before its callable
    # runs, and clears the slot.
    log.clear()
    _script_ping(session, iter([_busy(), KipyConnectionError("t"), None]), log)
    assert await session.call(lambda h: log.append("fn") or "done", timeout=100) == "done"
    assert log == ["Ping", "Ping", "Ping", "fn"]
    assert session.refill_busy is None

    log.clear()  # idle now: no probing any more
    await session.call(lambda h: log.append("fn"))
    assert log == ["fn"]


async def test_busy_wait_bounded_by_call_budget(session: KicadSession, clock: Clock) -> None:
    """Pings that hang for the transport timeout are only sent while they end before the
    call's own deadline (minus the margin): then `refill_timeout`, callable not run,
    slot kept, no restart."""
    log: list[str] = []
    _script_ping(session, _always(HANG), log, clock)
    session.refill_busy = session.generation
    restarts = session.restart_count
    start = clock.now
    with pytest.raises(StructuredToolError) as info:
        await session.call(lambda h: log.append("fn"), timeout=100)
    assert info.value.code == "refill_timeout"
    assert log == ["Ping", "Ping", "Ping"]  # ends at +90; a 4th would end at +120 > +99
    assert clock.now - start <= 100 - session_mod.IDLE_WAIT_MARGIN_S
    assert session.refill_busy == session.generation
    assert session.restart_count == restarts

    # A call whose budget (2 s in these settings) cannot fit one 30 s Ping refuses at once.
    log.clear()
    with pytest.raises(StructuredToolError) as info:
        await session.call(lambda h: log.append("fn"))
    assert info.value.code == "refill_timeout" and log == []
    assert session.refill_busy == session.generation and session.restart_count == restarts


async def test_busy_wait_short_probes(session: KicadSession, clock: Clock) -> None:
    """With the kipy client's private parts present the probes block ≤ 2 s each, so a
    10 s call can afford several: 4 hung probes (ending at +8 ≤ +9), then
    `refill_timeout`; every socket restored or closed."""
    client = FakeClient(clock, _always(HANG), timeout_ms=30_000)
    session._kicad._client = client
    session._kicad.ping = lambda: client.send(Ping(), Empty)
    session.refill_busy = session.generation
    start = clock.now
    with pytest.raises(StructuredToolError) as info:
        await session.call(lambda h: None, timeout=10)
    assert info.value.code == "refill_timeout"
    assert client.ping_timeouts == [2_000] * 4
    assert clock.now - start == pytest.approx(8.0)
    _sockets_clean(client)


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
        await session.call(lambda h: log.append("fn"), timeout=100)
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
