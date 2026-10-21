import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from copperchat.projects import Project
from copperchat.repo_context.listing import clear_listing_cache
from copperchat.repo_context.paths import clear_repo_policy_cache


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=t@example.com", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    return path


def make_project(project_dir: Path) -> Project:
    project_dir.mkdir(parents=True, exist_ok=True)
    pro = project_dir / f"{project_dir.name}.kicad_pro"
    pro.touch()
    return Project(
        id=project_dir.name,
        name=project_dir.name,
        rel_path=project_dir.name,
        dir=project_dir,
        pro_file=pro,
        sch_file=None,
        pcb_file=None,
    )


@pytest.fixture(autouse=True)
def _clear_caches() -> Iterator[None]:
    clear_repo_policy_cache()
    clear_listing_cache()
    yield
    clear_repo_policy_cache()
    clear_listing_cache()
