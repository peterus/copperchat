from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import SecretStr

from copperchat.config import Settings
from copperchat.db import Database
from copperchat.projects import Project

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
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        projects_root=tmp_path / "projects",
        data_dir=tmp_path / "data",
        claude_code_oauth_token=SecretStr("tok"),
    )


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    d = await Database.open(tmp_path / "t.db")
    yield d
    await d.close()


async def public_resolver(host: str, port: int) -> list[str]:
    """Test DNS: *.internal and localhost are private, everything else public."""
    if host in ("localhost",) or host.endswith(".internal"):
        return ["127.0.0.1"] if host == "localhost" else ["10.0.0.5"]
    return ["93.184.216.34"]
