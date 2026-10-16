import hashlib
import shutil
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from copperchat.config import Settings
from copperchat.kicad_session import KicadSession, KicadSessionManager, KicadUnavailable
from copperchat.projects import Project, discover_projects
from tests.support.kicad_env import KICAD_CLI, kicad_demo_dir

pytestmark = pytest.mark.kicad


def _demo_project(settings: Settings, demo: str, name: str) -> Project:
    demos = kicad_demo_dir()
    if demos is None or not (demos / demo).is_dir():
        pytest.skip(f"KiCad demo {demo!r} not installed next to kicad-cli")
    shutil.copytree(demos / demo, settings.projects_root / demo)  # verbatim copy
    return next(p for p in discover_projects(settings.projects_root) if p.name == name)


def _hashes(directory: Path) -> dict[str, str]:
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file() and not p.name.endswith(".lck")
    }


@pytest.fixture
def real_settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings(kicad_cli=KICAD_CLI, kicad_start_timeout_s=60.0, kicad_api_timeout_s=30.0)


@pytest.fixture
async def manager(real_settings: Settings) -> AsyncIterator[KicadSessionManager]:
    m = KicadSessionManager(real_settings)
    yield m
    await m.close_all()


async def test_start_ping_close(real_settings: Settings, manager: KicadSessionManager) -> None:
    project = _demo_project(real_settings, "ecc83", "ecc83-pp")
    session = await manager.open(project)
    await session.call(lambda h: h.kicad.ping())
    version = await session.call(lambda h: h.kicad.get_version().full_version)
    assert version
    process = session.process
    await manager.close_all()
    assert process is not None and process.returncode is not None
    assert not (project.dir / f"~{project.pro_file.name}.lck").exists()


async def test_documents_are_open(real_settings: Settings, manager: KicadSessionManager) -> None:
    session = await manager.open(_demo_project(real_settings, "ecc83", "ecc83-pp"))
    symbols = await session.call(lambda h: len(h.schematic().get_symbols()))
    footprints = await session.call(lambda h: len(h.board().get_footprints()))
    assert symbols > 0 and footprints > 0


async def test_own_lock_does_not_count_as_foreign(
    real_settings: Settings, manager: KicadSessionManager
) -> None:
    project = _demo_project(real_settings, "ecc83", "ecc83-pp")
    session: KicadSession = await manager.open(project)
    assert session.read_only is False
    assert (project.dir / f"~{project.pro_file.name}.lck").exists()
    assert manager.lock_status(project).locked is False


async def test_kill_restarts_and_next_call_works(
    real_settings: Settings, manager: KicadSessionManager
) -> None:
    session = await manager.open(_demo_project(real_settings, "ecc83", "ecc83-pp"))
    assert session.process is not None
    session.process.kill()
    with pytest.raises(KicadUnavailable):
        await session.call(lambda h: h.kicad.ping())
    assert session.restart_count == 1
    assert await session.call(lambda h: len(h.schematic().get_symbols())) > 0
    # The stale lock left by kill -9 was replaced by our new server's own lock.
    assert manager.lock_status(session.project).locked is False


async def test_save_copy_of_hierarchical_schematic(
    real_settings: Settings, manager: KicadSessionManager, tmp_path: Path
) -> None:
    project = _demo_project(real_settings, "complex_hierarchy", "complex_hierarchy")
    before = _hashes(project.dir)
    session = await manager.open(project)
    assert project.sch_file is not None
    dest = tmp_path / "copy" / project.sch_file.name
    await session.save_copy("sch", dest)
    sheets = await session.call(lambda h: {s.filename for s in h.schematic().get_hierarchy()})
    for name in sheets:
        assert (dest.parent / name).is_file(), name
    await manager.close_all()
    assert _hashes(project.dir) == before  # the user's project stays byte-identical
