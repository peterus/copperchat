"""Real api-server sessions on temporary copies of the committed fixture projects."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from copperchat.config import Settings
from copperchat.kicad_session import KicadSession, KicadSessionManager
from copperchat.projects import discover_projects
from tests.support.kicad_env import KICAD_CLI

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "projects"


def copy_fixture(name: str, dest_root: Path) -> Path:
    dest = dest_root / name
    shutil.copytree(FIXTURES / name, dest)  # verbatim copy; tests never write the originals
    return dest


def real_settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings(kicad_cli=KICAD_CLI, kicad_api_timeout_s=30.0, kicad_start_timeout_s=60.0)


async def _open(
    settings: Settings, name: str, stem: str, *, drop_board: bool = False
) -> AsyncIterator[KicadSession]:
    dest = copy_fixture(name, settings.projects_root)
    if drop_board:  # a schematic-only project (owner decision 3/4)
        for pcb in dest.glob("*.kicad_pcb"):
            pcb.unlink()
    project = next(p for p in discover_projects(settings.projects_root) if p.name == stem)
    manager = KicadSessionManager(settings)
    try:
        yield await manager.open(project)
    finally:
        await manager.close_all()


@pytest.fixture
async def ecc83_session(make_settings: Callable[..., Settings]) -> AsyncIterator[KicadSession]:
    async for session in _open(real_settings(make_settings), "ecc83", "ecc83-pp"):
        yield session


@pytest.fixture
async def hier_session(make_settings: Callable[..., Settings]) -> AsyncIterator[KicadSession]:
    async for session in _open(
        real_settings(make_settings), "complex_hierarchy", "complex_hierarchy"
    ):
        yield session


@pytest.fixture
async def blank_session(make_settings: Callable[..., Settings]) -> AsyncIterator[KicadSession]:
    async for session in _open(real_settings(make_settings), "blank", "blank"):
        yield session


@pytest.fixture
async def blank_nopcb_session(
    make_settings: Callable[..., Settings],
) -> AsyncIterator[KicadSession]:
    """The blank project copied without its .kicad_pcb."""
    async for session in _open(real_settings(make_settings), "blank", "blank", drop_board=True):
        yield session
