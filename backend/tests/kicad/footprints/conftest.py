"""Fixtures of the real-KiCad footprint tests."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from copperchat.config import Settings
from copperchat.db import Database
from copperchat.kicad_session import KicadSession, KicadSessionManager
from copperchat.projects import discover_projects
from tests.support.kicad_project import FIXTURES, real_settings

REPO = "OpenDriveHub"
SessionFactory = Callable[..., Awaitable[KicadSession]]


def make_repo(settings: Settings) -> Path:
    """The git repo `<projects_root>/OpenDriveHub` with `hardware/main` and `hardware/io`,
    each a verbatim copy of the KiCad-generated blank project without its board."""
    root = settings.projects_root / REPO
    for name in ("main", "io"):
        dest = root / "hardware" / name
        shutil.copytree(FIXTURES / "blank", dest)
        for pcb in dest.glob("*.kicad_pcb"):
            pcb.unlink()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


@pytest.fixture
async def repo_session(
    make_settings: Callable[..., Settings],
) -> AsyncIterator[tuple[SessionFactory, Path]]:
    """A git repo `<projects_root>/OpenDriveHub` with `hardware/main` and `hardware/io`,
    each a verbatim copy of the KiCad-generated blank project without its board (spike
    3A). Yields `(open_session, repo_root)`: `await open_session("main" | "io")` opens
    that project's api-server; one server at a time (C14), so opening one closes the
    previous."""
    settings = real_settings(make_settings)
    root = make_repo(settings)
    manager = KicadSessionManager(settings)

    async def open_session(name: str = "main") -> KicadSession:
        await manager.close_all()
        project_dir = (root / "hardware" / name).resolve()
        project = next(
            p for p in discover_projects(settings.projects_root) if p.dir.resolve() == project_dir
        )
        return await manager.open(project)

    try:
        yield open_session, root
    finally:
        await manager.close_all()


@pytest.fixture
async def edit_db(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.open(tmp_path / "edit.db")
    try:
        yield db
    finally:
        await db.close()
