import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from copperchat.config import Settings
from copperchat.errors import CopperChatError, DocumentNotOpen, ReadOnlyProject
from copperchat.kicad_session import KicadSessionManager, KicadUnavailable, socket_path_for
from copperchat.projects import Project, discover_projects, lock_status
from tests.support.fake_kicad import FakeClientFactory, FakeKiCad, FakeLauncher, FakeServer


def make_project(root: Path, locked: bool = False, folder: str = "demo") -> Project:
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    # filename markers only, never opened (approved by owner 2026-10-16)
    for suffix in ("kicad_pro", "kicad_sch", "kicad_pcb"):
        (d / f"demo.{suffix}").touch()
    if locked:
        (d / "~demo.kicad_pcb.lck").touch()
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


async def test_open_starts_server_and_connects(
    manager: KicadSessionManager, server: FakeServer, make_settings: Callable[..., Settings]
) -> None:
    project = make_project(make_settings().projects_root)
    session = await manager.open(project)
    assert manager.current() is session
    assert server.starts == 1
    assert session.read_only is False
    assert session.socket_path == socket_path_for(project)
    assert session.socket_path.parent.stat().st_mode & 0o777 == 0o700
    version = await session.call(lambda h: h.kicad.get_version().full_version)
    assert version == "11.0.0-fake"


async def test_reopening_same_project_reuses_session(
    manager: KicadSessionManager, server: FakeServer, make_settings: Callable[..., Settings]
) -> None:
    project = make_project(make_settings().projects_root)
    a = await manager.open(project)
    b = await manager.open(project)
    assert a is b and server.starts == 1


async def test_opening_other_project_closes_previous(
    manager: KicadSessionManager, server: FakeServer, tmp_path: Path
) -> None:
    # Ids derive from the path relative to the root, so use different folders.
    first = make_project(tmp_path / "r1", folder="one")
    second = make_project(tmp_path / "r2", folder="two")
    s1 = await manager.open(first)
    proc = s1.process
    s2 = await manager.open(second)
    assert manager.current() is s2
    assert proc is not None and proc.returncode is not None
    with pytest.raises(KicadUnavailable):
        await s1.call(lambda h: h.kicad.ping())


async def test_locked_project_is_read_only_and_refuses_save(
    manager: KicadSessionManager, make_settings: Callable[..., Settings]
) -> None:
    project = make_project(make_settings().projects_root, locked=True)
    session = await manager.open(project)
    assert session.read_only is True
    with pytest.raises(ReadOnlyProject):
        await session.save("pcb")


async def test_start_fails_when_process_exits(
    make_settings: Callable[..., Settings], server: FakeServer
) -> None:
    server.fail_start_with_exit_code = 3
    m = KicadSessionManager(
        make_settings(), FakeLauncher(server), client_factory=FakeClientFactory(server)
    )
    with pytest.raises(KicadUnavailable, match="exited with code 3"):
        await m.open(make_project(make_settings().projects_root))
    assert m.current() is None


async def test_start_times_out_when_never_ready(
    make_settings: Callable[..., Settings], server: FakeServer
) -> None:
    server.dead = True

    class NeverReady(FakeClientFactory):
        def __call__(self, socket_uri: str, timeout_ms: int):  # type: ignore[no-untyped-def]
            client = super().__call__(socket_uri, timeout_ms)
            self.server.dead = True
            return client

    m = KicadSessionManager(
        make_settings(kicad_start_timeout_s=0.5),
        FakeLauncher(server),
        client_factory=NeverReady(server),
    )
    with pytest.raises(KicadUnavailable, match="not ready"):
        await m.open(make_project(make_settings().projects_root))


async def test_open_opens_schematic_and_board(
    manager: KicadSessionManager, server: FakeServer, make_settings: Callable[..., Settings]
) -> None:
    # Spike 1C: the api-server only preloads the project; documents need open_document.
    from kipy.proto.common.types.base_types_pb2 import DocumentType

    await manager.open(make_project(make_settings().projects_root))
    assert server.opened == [
        ("demo.kicad_sch", DocumentType.DOCTYPE_SCHEMATIC),
        ("demo.kicad_pcb", DocumentType.DOCTYPE_PCB),
    ]


async def test_handle_refuses_document_the_project_lacks(
    manager: KicadSessionManager, make_settings: Callable[..., Settings]
) -> None:
    project = make_project(make_settings().projects_root)
    project.pcb_file.unlink()  # type: ignore[union-attr]
    session = await manager.open(discover_projects(make_settings().projects_root)[0])
    with pytest.raises(DocumentNotOpen):
        await session.call(lambda h: h.board())
    del project


async def test_own_lock_is_not_reported_as_foreign(
    manager: KicadSessionManager, make_settings: Callable[..., Settings]
) -> None:
    project = make_project(make_settings().projects_root)
    await manager.open(project)
    own_lock = project.dir / "~demo.kicad_pro.lck"
    assert own_lock.exists()  # written by the (fake) api-server
    assert lock_status(project).locked is True
    assert manager.lock_status(project).locked is False


async def test_stale_own_lock_after_crash_does_not_make_project_read_only(
    make_settings: Callable[..., Settings], server: FakeServer
) -> None:
    settings = make_settings()
    project = make_project(settings.projects_root)
    first = KicadSessionManager(
        settings, FakeLauncher(server), client_factory=FakeClientFactory(server)
    )
    await first.open(project)
    server.crash()  # kill -9: the lock file stays behind
    await first.close_all()
    assert (project.dir / "~demo.kicad_pro.lck").exists()
    second = KicadSessionManager(
        settings, FakeLauncher(server), client_factory=FakeClientFactory(server)
    )
    try:
        session = await second.open(project)
        assert session.read_only is False
    finally:
        await second.close_all()


async def test_restart_records_new_own_lock(
    manager: KicadSessionManager, make_settings: Callable[..., Settings]
) -> None:
    project = make_project(make_settings().projects_root)
    session = await manager.open(project)
    await session.restart()
    assert manager.lock_status(project).locked is False


async def test_foreign_kicad_lock_makes_read_only(
    manager: KicadSessionManager, server: FakeServer, make_settings: Callable[..., Settings]
) -> None:
    project = make_project(make_settings().projects_root)
    lock = project.dir / "~demo.kicad_pro.lck"
    lock.write_text('{"hostname": "desk", "token": "gui-token", "username": "me"}')
    server.write_lock = False
    session = await manager.open(project)
    assert session.read_only is True
    assert session.lock_files == (lock.resolve(),)
    assert manager.lock_status(project).locked is True


async def test_save_copy_board(
    manager: KicadSessionManager, make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    session = await manager.open(make_project(make_settings().projects_root))
    dest = tmp_path / "copy" / "demo.kicad_pcb"
    assert await session.save_copy("pcb", dest) == dest
    assert dest.exists()


async def test_save_copy_schematic_copies_sub_sheets(
    manager: KicadSessionManager,
    server: FakeServer,
    make_settings: Callable[..., Settings],
    tmp_path: Path,
) -> None:
    # Spike 1C (Q4): save_as writes only the root sheet; sub-sheets are byte-copied.
    project = make_project(make_settings().projects_root)
    (project.dir / "sub").mkdir()
    (project.dir / "sub" / "amp.kicad_sch").write_bytes(b"")  # empty marker
    server.hierarchy = ["demo.kicad_sch", "sub/amp.kicad_sch", "sub/amp.kicad_sch"]
    session = await manager.open(project)
    dest = tmp_path / "view" / "demo.kicad_sch"
    await session.save_copy("sch", dest)
    assert dest.exists()
    assert (tmp_path / "view" / "sub" / "amp.kicad_sch").exists()
    assert server.saved == []  # never save(): the user's files stay untouched


async def test_save_copy_rejects_sheet_outside_project(
    manager: KicadSessionManager,
    server: FakeServer,
    make_settings: Callable[..., Settings],
    tmp_path: Path,
) -> None:
    project = make_project(make_settings().projects_root)
    server.hierarchy = ["demo.kicad_sch", "../../outside.kicad_sch"]
    session = await manager.open(project)
    with pytest.raises(CopperChatError, match="outside"):
        await session.save_copy("sch", tmp_path / "view" / "demo.kicad_sch")


async def test_socket_dir_must_not_be_a_symlink(
    manager: KicadSessionManager,
    make_settings: Callable[..., Settings],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from copperchat.kicad_session import session as session_module

    target = tmp_path / "attacker"
    target.mkdir()
    link = tmp_path / "sockets"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(session_module, "SOCKET_DIR", link)
    with pytest.raises(KicadUnavailable, match="private directory"):
        await manager.open(make_project(make_settings().projects_root))


async def test_socket_dir_is_created_private(
    manager: KicadSessionManager,
    make_settings: Callable[..., Settings],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from copperchat.kicad_session import session as session_module

    sockets = tmp_path / "fresh-sockets"
    monkeypatch.setattr(session_module, "SOCKET_DIR", sockets)
    session = await manager.open(make_project(make_settings().projects_root))
    assert session.socket_path.parent == sockets
    assert sockets.stat().st_mode & 0o777 == 0o700


async def test_cancelled_open_leaves_no_server_running(
    make_settings: Callable[..., Settings], server: FakeServer
) -> None:
    class NeverReady(FakeClientFactory):
        def __call__(self, socket_uri: str, timeout_ms: int) -> FakeKiCad:
            client = super().__call__(socket_uri, timeout_ms)
            self.server.dead = True
            return client

    m = KicadSessionManager(
        make_settings(kicad_start_timeout_s=30),
        FakeLauncher(server),
        client_factory=NeverReady(server),
    )
    task = asyncio.create_task(m.open(make_project(make_settings().projects_root)))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert server.processes and all(p.returncode is not None for p in server.processes)
    assert m.current() is None


async def test_restart_closes_the_old_client(
    manager: KicadSessionManager, server: FakeServer, make_settings: Callable[..., Settings]
) -> None:
    session = await manager.open(make_project(make_settings().projects_root))
    await session.restart()
    assert server.closed >= 1


async def test_reopen_re_evaluates_foreign_lock(
    manager: KicadSessionManager, make_settings: Callable[..., Settings]
) -> None:
    project = make_project(make_settings().projects_root)
    first = await manager.open(project)
    assert first.read_only is False
    gui_lock = project.dir / "~demo.kicad_sch.lck"
    gui_lock.touch()  # KiCad GUI opened the project meanwhile
    second = await manager.open(project)
    assert second is not first and second.read_only is True
    gui_lock.unlink()  # ... and closed it again
    third = await manager.open(project)
    assert third.read_only is False
    assert first.process is None and second.process is None
