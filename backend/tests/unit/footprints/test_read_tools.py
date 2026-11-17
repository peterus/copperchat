"""Footprint read path (F1) against the fake api-server."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from kipy.errors import ApiError
from kipy.proto.common.types.base_types_pb2 import DocumentType

from copperchat.config import Settings
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadSession, KicadSessionManager
from copperchat.kicad_session.footprint_doc import SEED_LIB_ID
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
