"""Fixtures for the design_docs package (C3): a temp project, settings, DB and service."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from pydantic import SecretStr

from copperchat.agent.broadcaster import Broadcaster
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.projects import Project

if TYPE_CHECKING:
    from copperchat.design_docs.service import DesignDocs

PID = "0123456789ab"


def make_project(d: Path, pid: str = PID) -> Project:
    d.mkdir(parents=True, exist_ok=True)
    return Project(
        id=pid,
        name=d.name,
        rel_path=f"{d.name}/{d.name}.kicad_pro",
        dir=d,
        pro_file=d / f"{d.name}.kicad_pro",
        sch_file=None,
        pcb_file=None,
    )


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return make_project(tmp_path / "projects" / "proj")


@pytest.fixture
def design(project: Project) -> Path:
    d = project.dir / "docs" / "design"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        projects_root=tmp_path / "projects",
        data_dir=tmp_path / "data",
        claude_code_oauth_token=SecretStr("tok"),
    )


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.open(tmp_path / "t.db")
    yield database
    await database.close()


@pytest.fixture
def broadcaster() -> Broadcaster:
    return Broadcaster()


@pytest.fixture
def docs(db: Database, settings: Settings, broadcaster: Broadcaster) -> DesignDocs:
    from copperchat.design_docs.service import DesignDocs

    return DesignDocs(db, settings, broadcaster)
