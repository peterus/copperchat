"""Per-document KiCad commits and modified state (SP4 P1a, Task 3)."""

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from copperchat.config import Settings
from copperchat.errors import DocumentNotOpen
from copperchat.kicad_session import KicadSessionManager
from copperchat.projects import Project, discover_projects
from tests.support.fake_kicad import FakeClientFactory, FakeLauncher, FakeServer


def make_project(root: Path, *, board: bool = True) -> Project:
    d = root / "demo"
    d.mkdir(parents=True, exist_ok=True)
    # filename markers only, never opened (approved by owner 2026-10-16)
    suffixes = ["kicad_pro", "kicad_sch"] + (["kicad_pcb"] if board else [])
    for suffix in suffixes:
        (d / f"demo.{suffix}").touch()
    return discover_projects(root)[0]


@pytest.fixture
def server() -> FakeServer:
    s = FakeServer()
    yield s
    s.release.set()


@pytest.fixture
async def manager(
    make_settings: Callable[..., Settings], server: FakeServer
) -> AsyncIterator[KicadSessionManager]:
    m = KicadSessionManager(
        make_settings(), FakeLauncher(server), client_factory=FakeClientFactory(server)
    )
    yield m
    await m.close_all()


async def test_pcb_commit_marks_pcb_only(
    manager: KicadSessionManager, server: FakeServer, make_settings: Callable[..., Settings]
) -> None:
    session = await manager.open(make_project(make_settings().projects_root))
    async with session.commit("move", doc="pcb"):
        pass
    assert server.commit_ops == [("begin", "demo.kicad_pcb"), ("push", "demo.kicad_pcb")]
    assert session.modified_docs == {"pcb"}
    assert session.modified is False
    assert session.jobs_stale is False


async def test_save_pcb_clears_pcb(
    manager: KicadSessionManager, server: FakeServer, make_settings: Callable[..., Settings]
) -> None:
    session = await manager.open(make_project(make_settings().projects_root))
    async with session.commit("sch edit"):
        pass
    async with session.commit("pcb edit", doc="pcb"):
        pass
    assert session.modified_docs == {"sch", "pcb"}
    await session.save("pcb")
    assert server.saved == ["demo.kicad_pcb"]
    assert session.modified_docs == {"sch"} and session.modified is True
    await session.save("sch")
    assert session.modified_docs == set() and session.modified is False


async def test_sch_commit_unchanged(
    manager: KicadSessionManager, server: FakeServer, make_settings: Callable[..., Settings]
) -> None:
    session = await manager.open(make_project(make_settings().projects_root))
    async with session.commit("wire"):
        pass
    assert server.commit_ops == [("begin", "demo.kicad_sch"), ("push", "demo.kicad_sch")]
    assert session.modified is True and session.jobs_stale is True
    assert session.modified_docs == {"sch"}


async def test_pcb_commit_without_board(
    manager: KicadSessionManager, server: FakeServer, make_settings: Callable[..., Settings]
) -> None:
    session = await manager.open(make_project(make_settings().projects_root, board=False))
    with pytest.raises(DocumentNotOpen):
        async with session.commit("move", doc="pcb"):
            pass
    assert server.commit_ops == []
    assert session.modified_docs == set()


async def test_pcb_commit_without_board_does_not_wait_for_the_lock(
    manager: KicadSessionManager, make_settings: Callable[..., Settings]
) -> None:
    session = await manager.open(make_project(make_settings().projects_root, board=False))
    async with session._commit_lock:  # held elsewhere: the check must come first
        with pytest.raises(DocumentNotOpen):
            async with session.commit("move", doc="pcb"):
                pass


async def test_drop_on_error_pcb(
    manager: KicadSessionManager, server: FakeServer, make_settings: Callable[..., Settings]
) -> None:
    session = await manager.open(make_project(make_settings().projects_root))
    with pytest.raises(RuntimeError, match="boom"):
        async with session.commit("move", doc="pcb"):
            raise RuntimeError("boom")
    assert server.commit_ops == [("begin", "demo.kicad_pcb"), ("drop", "demo.kicad_pcb")]
    assert server.open_commits == set()
    assert session.modified_docs == set()


async def test_pcb_dry_run_drops_on_board(
    manager: KicadSessionManager, server: FakeServer, make_settings: Callable[..., Settings]
) -> None:
    session = await manager.open(make_project(make_settings().projects_root))
    async with session.commit("probe", doc="pcb", dry_run=True):
        pass
    assert server.commit_ops == [("begin", "demo.kicad_pcb"), ("drop", "demo.kicad_pcb")]
    assert session.modified_docs == set()


async def test_restart_clears_modified_docs(
    manager: KicadSessionManager, make_settings: Callable[..., Settings]
) -> None:
    session = await manager.open(make_project(make_settings().projects_root))
    session.modified_docs.update({"sch", "pcb"})
    await session.restart()
    assert session.modified_docs == set() and session.modified is False


async def test_modified_is_read_only(
    manager: KicadSessionManager, make_settings: Callable[..., Settings]
) -> None:
    session = await manager.open(make_project(make_settings().projects_root))
    with pytest.raises(AttributeError):
        session.modified = True  # type: ignore[misc]
