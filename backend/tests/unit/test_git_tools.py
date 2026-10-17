import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from copperchat.config import Settings
from copperchat.git_tools.specs import COMMIT_INTENT, TOOL_SPECS, git_status_data
from copperchat.projects import discover_projects
from copperchat.tools import StructuredToolError, ToolContext
from copperchat.tools.registry import all_specs
from copperchat.web.app import create_app

SPECS = {s.name: s for s in TOOL_SPECS}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, timeout=30
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    proj = root / "boards" / "demo"
    proj.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "config", "commit.gpgsign", "false")
    for suffix in ("kicad_pro", "kicad_sch"):
        (proj / f"demo.{suffix}").touch()  # empty markers (owner decision 5)
    (root / "README.md").write_text("outside project\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root


def ctx(project_dir: Path, user_message: str | None) -> ToolContext:
    project = discover_projects(project_dir.parent)[0]
    return ToolContext(
        project=project,
        session=cast(Any, None),
        settings=cast(Any, None),
        user_message=user_message,
    )


def make_changes(repo: Path) -> Path:
    proj = repo / "boards" / "demo"
    (proj / "notes.txt").write_text("rev B\n")
    for sub in ("history", "view", "renders", "uploads", "images"):
        (proj / ".kicad-agent" / sub).mkdir(parents=True)
        (proj / ".kicad-agent" / sub / "x.bin").write_bytes(b"x")
    (proj / "~demo.kicad_pro.lck").write_text("{}")  # KiCad / api-server lock file
    (repo / "README.md").write_text("changed outside\n")
    return proj


def test_registered_in_registry() -> None:
    names = {s.name for s in all_specs()}
    assert {"git_status", "git_diff_summary", "git_commit"} <= names
    assert all(not s.mutating and not s.changes_documents for s in TOOL_SPECS)


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("Please commit the current changes. Suggested message: rev B", True),
        ("COMMIT this", True),
        ("commit", True),
        ("what did I commit to?", True),
        ("I'm committed to this design", False),
        ("recommit", False),
        ("show git status", False),
    ],
)
def test_commit_intent(text: str, ok: bool) -> None:
    assert bool(COMMIT_INTENT.search(text)) is ok


async def test_status_and_diff_limited_to_project(repo: Path) -> None:
    proj = make_changes(repo)
    (proj / "demo.kicad_sch").write_text("x\ny\n")  # byte change of a tracked marker
    data = (await SPECS["git_status"].handler(ctx(proj, None), {})).data
    assert data is not None and data["branch"] == "main" and data["clean"] is False
    assert data["files"] == [
        {"path": "boards/demo/demo.kicad_sch", "status": "M"},
        {"path": "boards/demo/notes.txt", "status": "??"},
    ]
    diff = (await SPECS["git_diff_summary"].handler(ctx(proj, None), {})).data
    assert diff is not None and diff["count"] == 2
    assert diff["files"][0] == {
        "path": "boards/demo/demo.kicad_sch",
        "status": "M",
        "added": 2,
        "deleted": 0,
    }
    assert diff["files"][1]["added"] is None  # untracked: not in `git diff HEAD`


async def test_rename_is_reported_once(repo: Path) -> None:
    proj = repo / "boards" / "demo"
    (proj / "a.txt").write_text("same content\n" * 5)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "a")
    git(repo, "mv", "boards/demo/a.txt", "boards/demo/b.txt")
    data = await git_status_data(proj)
    assert data["files"] == [{"path": "boards/demo/b.txt", "status": "R"}]
    diff = (await SPECS["git_diff_summary"].handler(ctx(proj, None), {})).data
    assert diff is not None and diff["files"][0]["path"] == "boards/demo/b.txt"


async def test_commit_requires_explicit_user_request(repo: Path) -> None:
    proj = make_changes(repo)
    for message in ("looks good, thanks", None, "I'm committed to it"):
        with pytest.raises(StructuredToolError) as err:
            await SPECS["git_commit"].handler(ctx(proj, message), {"message": "rev B"})
        assert "commit" in err.value.to_dict()["why"]
    assert git(repo, "rev-list", "--count", "HEAD").strip() == "1"


async def test_commit_only_project_files_never_push(repo: Path) -> None:
    proj = make_changes(repo)
    (repo / "staged-outside.txt").write_text("x\n")
    git(repo, "add", "staged-outside.txt")
    res = (
        await SPECS["git_commit"].handler(
            ctx(proj, "Please commit the current changes. Suggested message: rev B"),
            {"message": "rev B"},
        )
    ).data
    assert res is not None and len(res["commit"]) == 40
    assert res["files"] == ["boards/demo/notes.txt"]
    committed = git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == ["boards/demo/notes.txt"]
    status = git(repo, "status", "--porcelain")
    assert " M README.md" in status and "A  staged-outside.txt" in status  # untouched
    assert git(repo, "log", "-1", "--format=%s").strip() == "rev B"
    assert git(repo, "remote").strip() == ""
    assert git(repo, "rev-list", "--count", "HEAD").strip() == "2"


async def test_commit_skips_repo_hooks(repo: Path) -> None:
    proj = make_changes(repo)
    hook = repo / ".git" / "hooks" / "post-commit"
    marker = repo / "hook-ran"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    await SPECS["git_commit"].handler(ctx(proj, "commit please"), {"message": "x"})
    assert not marker.exists()


async def test_commit_message_validated(repo: Path) -> None:
    proj = make_changes(repo)
    for message in ("", "   ", "x" * 2001):
        with pytest.raises(StructuredToolError):
            await SPECS["git_commit"].handler(ctx(proj, "commit"), {"message": message})


async def test_commit_with_nothing_to_commit_is_error(repo: Path) -> None:
    proj = repo / "boards" / "demo"
    with pytest.raises(StructuredToolError) as err:
        await SPECS["git_commit"].handler(ctx(proj, "commit please"), {"message": "x"})
    assert err.value.what == "Nothing to commit"


async def test_outside_git_repo_refused(tmp_path: Path) -> None:
    proj = tmp_path / "plain" / "demo"
    proj.mkdir(parents=True)
    (proj / "demo.kicad_pro").touch()
    for name in ("git_status", "git_diff_summary"):
        with pytest.raises(StructuredToolError) as err:
            await SPECS[name].handler(ctx(proj, None), {})
        assert "git repository" in err.value.to_dict()["what"]
    with pytest.raises(StructuredToolError):
        await SPECS["git_commit"].handler(ctx(proj, "commit"), {"message": "x"})
    assert await git_status_data(proj) == {
        "isRepo": False,
        "branch": None,
        "clean": True,
        "files": [],
    }


def test_git_status_route(make_settings: Callable[..., Settings], repo: Path) -> None:
    settings = make_settings(projects_root=repo, dev_fake_agent=True)
    make_changes(repo)
    with TestClient(create_app(settings)) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": "test-password"})
        pid = discover_projects(repo)[0].id
        body = c.get(f"/api/projects/{pid}/git/status").json()
    assert body == {
        "isRepo": True,
        "branch": "main",
        "clean": False,
        "files": [{"path": "boards/demo/notes.txt", "status": "??"}],
    }


async def test_git_timeout_is_a_structured_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from copperchat.git_tools import specs

    monkeypatch.setattr(specs, "_TIMEOUT_S", 1e-9)
    with pytest.raises(StructuredToolError) as err:
        await git_status_data(repo / "boards" / "demo")
    assert "timed out" in err.value.what
