"""Footprint read path (F1) against the fake api-server."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from kipy.errors import ApiError
from kipy.proto.common.types.base_types_pb2 import DocumentType

from copperchat.config import Settings
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadSession, KicadSessionManager
from copperchat.kicad_session.footprint_doc import OPEN_TIMEOUT_S, SEED_LIB_ID
from copperchat.projects import Project, discover_projects
from tests.support.fake_kicad import FakeClientFactory, FakeKiCad, FakeLauncher, FakeServer

FP = int(DocumentType.DOCTYPE_FOOTPRINT)


def _project(root: Path) -> Project:
    d = root / "demo"
    d.mkdir(parents=True, exist_ok=True)
    # filename markers only, never opened (as in test_kicad_session)
    for suffix in ("kicad_pro", "kicad_sch"):
        (d / f"demo.{suffix}").touch()
    return discover_projects(root)[0]


@pytest.fixture
def server() -> FakeServer:
    s = FakeServer()
    yield s
    s.release.set()


async def _session(
    make_settings: Callable[..., Settings], server: FakeServer, **overrides: Any
) -> AsyncIterator[KicadSession]:
    settings = make_settings(**overrides)
    manager = KicadSessionManager(
        settings, FakeLauncher(server), client_factory=FakeClientFactory(server)
    )
    try:
        yield await manager.open(_project(settings.projects_root))
    finally:
        await manager.close_all()


@pytest.fixture
async def session(
    make_settings: Callable[..., Settings], server: FakeServer
) -> AsyncIterator[KicadSession]:
    async for s in _session(make_settings, server):
        yield s


@pytest.fixture
async def disabled_session(
    make_settings: Callable[..., Settings], server: FakeServer
) -> AsyncIterator[KicadSession]:
    async for s in _session(make_settings, server, footprint_tools=False):
        yield s


def _footprint_opens(server: FakeServer) -> list[tuple[str, int]]:
    return [o for o in server.opened if o[1] == FP]


async def test_activation_refused_when_disabled(
    disabled_session: KicadSession, server: FakeServer
) -> None:
    with pytest.raises(StructuredToolError) as err:
        await disabled_session.activate_footprints()
    assert err.value.code == "footprint_tools_disabled"
    assert _footprint_opens(server) == []
    assert not disabled_session.footprints_active


async def test_document_refused_when_disabled(
    disabled_session: KicadSession, server: FakeServer
) -> None:
    with pytest.raises(StructuredToolError) as err:
        async with disabled_session.footprint_document(SEED_LIB_ID):
            pass
    assert err.value.code == "footprint_tools_disabled"
    assert _footprint_opens(server) == []


async def test_activation_opens_and_closes_the_seed_once(
    session: KicadSession, server: FakeServer
) -> None:
    seconds = await session.activate_footprints()
    assert isinstance(seconds, float) and seconds >= 0
    assert session.footprints_active
    assert _footprint_opens(server) == [(SEED_LIB_ID, FP)]
    assert server.closed_docs == [SEED_LIB_ID]
    assert await session.activate_footprints() is None
    assert len(_footprint_opens(server)) == 1


async def test_activation_resets_on_restart(session: KicadSession) -> None:
    await session.activate_footprints()
    await session.restart()
    assert not session.footprints_active


async def test_document_closed_after_error(session: KicadSession, server: FakeServer) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with session.footprint_document(SEED_LIB_ID):
            raise RuntimeError("boom")
    assert server.closed_docs == [SEED_LIB_ID]
    assert not session.restart_pending


async def test_failed_close_sets_restart_pending(
    session: KicadSession, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: FakeKiCad, document: Any) -> None:
        raise ApiError("KiCad returned error: Requested document does not match")

    monkeypatch.setattr(FakeKiCad, "close_document", refuse)
    assert not session.restart_pending
    async with session.footprint_document(SEED_LIB_ID):
        pass
    assert session.restart_pending


async def test_long_timeout_only_for_the_activating_open(
    session: KicadSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeouts: list[float | None] = []
    real_call = session.call

    async def spy(fn: Any, *, timeout: float | None = None) -> Any:
        timeouts.append(timeout)
        return await real_call(fn, timeout=timeout)

    monkeypatch.setattr(session, "call", spy)
    await session.activate_footprints()
    assert timeouts[0] == OPEN_TIMEOUT_S
    timeouts.clear()
    async with session.footprint_document(SEED_LIB_ID):
        pass
    assert OPEN_TIMEOUT_S not in timeouts


def _refuse_footprint_open(monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    real_open = FakeKiCad.open_document

    def refuse(self: FakeKiCad, path: str, type: int) -> Any:
        if type == FP:
            raise ApiError(f"KiCad returned error: {message}")
        return real_open(self, path, type)

    monkeypatch.setattr(FakeKiCad, "open_document", refuse)


async def test_seed_not_found_is_seed_missing(
    session: KicadSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_footprint_open(monkeypatch, f"Footprint not found: {SEED_LIB_ID}")
    with pytest.raises(StructuredToolError) as err:
        await session.activate_footprints()
    assert err.value.code == "seed_missing"
    assert SEED_LIB_ID in str(err.value)
    assert not session.footprints_active


async def test_other_seed_error_is_library_error(
    session: KicadSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_footprint_open(monkeypatch, "library load failed")
    with pytest.raises(StructuredToolError) as err:
        await session.activate_footprints()
    assert err.value.code == "library_error"
    assert "library load failed" in str(err.value)


async def test_cancel_during_close_completes_the_close(
    session: KicadSession, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, reached = threading.Event(), threading.Event()
    real_close = FakeKiCad.close_document

    def gated(self: FakeKiCad, document: Any) -> None:
        reached.set()
        gate.wait(10)
        real_close(self, document)

    monkeypatch.setattr(FakeKiCad, "close_document", gated)

    async def use() -> None:
        async with session.footprint_document(SEED_LIB_ID):
            pass

    task = asyncio.create_task(use())
    assert await asyncio.to_thread(reached.wait, 5)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()  # the close still runs; the lock is still held
    assert session._fp_lock.locked()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert server.closed_docs == [SEED_LIB_ID]
    assert not session._fp_lock.locked()
    assert not session.restart_pending
    assert session.server_alive


async def test_activation_resets_on_kill(session: KicadSession) -> None:
    await session.activate_footprints()
    assert session.footprints_active
    await session._kill()  # e.g. a failed restart: no server, generation unchanged
    assert not session.footprints_active


async def test_cancel_during_open_closes_the_document(
    session: KicadSession, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    await session.activate_footprints()
    server.closed_docs.clear()
    gate, reached = threading.Event(), threading.Event()
    real_open = FakeKiCad.open_document

    def gated(self: FakeKiCad, path: str, type: int) -> Any:
        if type == FP:
            reached.set()
            gate.wait(10)
        return real_open(self, path, type)

    monkeypatch.setattr(FakeKiCad, "open_document", gated)
    entered: list[bool] = []

    async def use() -> None:
        async with session.footprint_document(SEED_LIB_ID):
            entered.append(True)

    task = asyncio.create_task(use())
    assert await asyncio.to_thread(reached.wait, 5)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()  # the open runs to completion
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert entered == []  # the body never ran
    assert server.closed_docs == [SEED_LIB_ID]
    assert not session._fp_lock.locked()
    assert not session.restart_pending


async def test_concurrent_activation_opens_the_seed_once(
    session: KicadSession, server: FakeServer
) -> None:
    results = await asyncio.gather(session.activate_footprints(), session.activate_footprints())
    assert sum(r is not None for r in results) == 1
    assert _footprint_opens(server) == [(SEED_LIB_ID, FP)]
    assert server.closed_docs == [SEED_LIB_ID]
