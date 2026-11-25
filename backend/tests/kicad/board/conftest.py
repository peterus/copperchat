"""Board (SP4) tests against a real KiCad api-server."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from copperchat.config import Settings
from copperchat.kicad_session import KicadSession, KicadSessionManager
from copperchat.projects import discover_projects
from tests.support.kicad_env import KICAD_CLI
from tests.support.kicad_project import copy_fixture


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def ecc83_module_session(
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[KicadSession]:
    """One ecc83 api-server for a whole test module (tests that only read, or whose
    changes the later tests of the module tolerate). Tests using it run on the module
    event loop: `pytest.mark.asyncio(loop_scope="module")`."""
    tmp: Path = tmp_path_factory.mktemp("ecc83-module")
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        projects_root=tmp / "projects",
        data_dir=tmp / "data",
        admin_password="test-password",
        claude_code_oauth_token="test-token",
        cookie_secure=False,
        frontend_dist=tmp / "no-frontend",
        kicad_cli=KICAD_CLI,
        kicad_api_timeout_s=30.0,
        kicad_start_timeout_s=60.0,
    )
    settings.projects_root.mkdir(parents=True)
    copy_fixture("ecc83", settings.projects_root)
    project = next(p for p in discover_projects(settings.projects_root) if p.name == "ecc83-pp")
    manager = KicadSessionManager(settings)
    try:
        yield await manager.open(project)
    finally:
        await manager.close_all()
