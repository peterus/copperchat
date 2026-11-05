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
        ("Committe regelmäßig", True),
        ("bitte committen", True),
        ("kannst du das commiten?", True),
        ("commite die Änderungen", True),
        ("er committet", True),
        ("committing now", True),
        ("uncommitted changes", False),
        ("the committee", False),
        ("show me the commits", False),
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
        "total": 1,
        "truncated": False,
    }


async def test_git_timeout_is_a_structured_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from copperchat.git_tools import specs

    monkeypatch.setattr(specs, "_TIMEOUT_S", 1e-9)
    with pytest.raises(StructuredToolError) as err:
        await git_status_data(repo / "boards" / "demo")
    assert "timed out" in err.value.what


async def test_file_list_is_capped(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from copperchat.git_tools import specs

    monkeypatch.setattr(specs, "FILES_MAX", 2)
    proj = repo / "boards" / "demo"
    for i in range(5):
        (proj / f"n{i}.txt").write_text("x\n")
    data = await git_status_data(proj)
    assert len(data["files"]) == 2 and data["total"] == 5 and data["truncated"] is True
    diff = (await SPECS["git_diff_summary"].handler(ctx(proj, None), {})).data
    assert diff is not None and len(diff["files"]) == 2 and diff["count"] == 5


async def test_git_env_ignores_stray_git_variables(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    data = await git_status_data(repo / "boards" / "demo")
    assert data["isRepo"] is True and data["branch"] == "main"


async def test_dubious_ownership_is_explained(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    from copperchat.git_tools import specs

    async def fake_state(project_dir: Path) -> tuple[bool, str]:
        return False, "fatal: detected dubious ownership in repository at '/projects'"

    monkeypatch.setattr(specs, "_repo_state", fake_state)
    data = await git_status_data(repo)
    assert data["isRepo"] is False and "uid 1000" in data["error"]
    with pytest.raises(StructuredToolError) as err:
        await SPECS["git_status"].handler(ctx(repo / "boards" / "demo", None), {})
    assert "dubious" in err.value.why


async def test_commit_with_ignored_cache_dirs_in_subdir_project(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    proj = root / "hardware" / "tx" / "pcb" / "main"
    (proj / ".kicad-agent").mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "config", "commit.gpgsign", "false")
    (proj / ".kicad-agent" / ".gitignore").write_text("history/\nimages/\nview/\nrenders/\n")
    for name in ("main.kicad_pro", "main.kicad_sch", "gone.txt", "mod.txt"):
        (proj / name).write_text("v1\n")
    (root / "outside.txt").write_text("v1\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    for sub in ("history", "images", "view", "renders"):
        (proj / ".kicad-agent" / sub).mkdir()
        (proj / ".kicad-agent" / sub / "x.bin").write_bytes(b"x")
    (proj / "new.txt").write_text("new\n")
    (proj / "mod.txt").write_text("v2\n")
    (proj / "gone.txt").unlink()
    (root / "outside.txt").write_text("v2\n")

    res = (
        await SPECS["git_commit"].handler(ctx(proj, "bitte committe das"), {"message": "rev B"})
    ).data
    assert res is not None
    base = "hardware/tx/pcb/main/"
    assert sorted(res["files"]) == [base + "gone.txt", base + "mod.txt", base + "new.txt"]
    assert git(root, "rev-list", "--count", "HEAD").strip() == "2"
    assert sorted(git(root, "show", "--name-only", "--format=", "HEAD").split()) == sorted(
        res["files"]
    )
    assert git(root, "status", "--porcelain").strip() == "M outside.txt"
    assert git(root, "ls-files", "--", ".kicad-agent").strip() == ""


async def test_commit_without_gitignore_still_excludes_cache_dirs(repo: Path) -> None:
    proj = make_changes(repo)
    res = (await SPECS["git_commit"].handler(ctx(proj, "commit"), {"message": "x"})).data
    assert res is not None and res["files"] == ["boards/demo/notes.txt"]
    tracked = git(repo, "ls-files").split()
    assert not any(".kicad-agent" in t or t.endswith(".lck") for t in tracked)


async def test_commit_with_only_ignored_changes_is_nothing_to_commit_up_front(repo: Path) -> None:
    proj = repo / "boards" / "demo"
    (proj / ".kicad-agent").mkdir()
    (proj / ".kicad-agent" / ".gitignore").write_text("*\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "ignore", "--allow-empty")
    (proj / ".kicad-agent" / "history").mkdir()
    (proj / ".kicad-agent" / "history" / "x.bin").write_bytes(b"x")
    with pytest.raises(StructuredToolError) as err:
        await SPECS["git_commit"].handler(ctx(proj, "commit"), {"message": "x"})
    # Ignored-only changes never reach `git add`: the pre-staging status guard fires
    # (the post-staging `diff --cached` check is a defensive second line).
    assert err.value.what == "Nothing to commit"
    assert "no changes" in err.value.to_dict()["why"]


# --- configurable identity (COPPERCHAT_GIT_NAME / COPPERCHAT_GIT_EMAIL) --------------------


def identity_ctx(
    project_dir: Path, name: str | None, email: str | None, message: str = "commit please"
) -> ToolContext:
    settings = Settings(_env_file=None, git_name=name, git_email=email)  # type: ignore[call-arg]
    return ToolContext(
        project=discover_projects(project_dir.parent)[0],
        session=cast(Any, None),
        settings=settings,
        user_message=message,
    )


@pytest.fixture
def no_host_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("EMAIL", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME"):
        monkeypatch.delenv(var, raising=False)


def unset_repo_identity(repo: Path, *keys: str) -> None:
    for key in keys:
        git(repo, "config", "--unset", key)


@pytest.mark.usefixtures("no_host_identity")
async def test_commit_without_any_identity_names_the_env_vars(repo: Path) -> None:
    unset_repo_identity(repo, "user.name", "user.email")
    proj = make_changes(repo)
    with pytest.raises(StructuredToolError) as err:
        await SPECS["git_commit"].handler(identity_ctx(proj, None, None), {"message": "x"})
    d = err.value.to_dict()
    assert "COPPERCHAT_GIT_NAME" in d["suggestion"] and "COPPERCHAT_GIT_EMAIL" in d["suggestion"]
    assert git(repo, "rev-list", "--count", "HEAD").strip() == "1"


@pytest.mark.usefixtures("no_host_identity")
async def test_commit_uses_configured_identity_for_author_and_committer(repo: Path) -> None:
    unset_repo_identity(repo, "user.name", "user.email")
    proj = make_changes(repo)
    res = await SPECS["git_commit"].handler(
        identity_ctx(proj, "Copper Bot", "bot@example.org"), {"message": "x"}
    )
    assert res.data is not None
    assert git(repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>").strip() == (
        "Copper Bot <bot@example.org>|Copper Bot <bot@example.org>"
    )
    persisted = subprocess.run(
        ["git", "-C", str(repo), "config", "--local", "--get-regexp", "^user\\."],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert persisted.stdout.strip() == ""  # nothing written to the repository


@pytest.mark.usefixtures("no_host_identity")
async def test_repo_identity_wins_over_settings(repo: Path) -> None:
    proj = make_changes(repo)  # repo fixture has user.name=Test, user.email=t@example.com
    await SPECS["git_commit"].handler(
        identity_ctx(proj, "Copper Bot", "bot@example.org"), {"message": "x"}
    )
    assert git(repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>").strip() == (
        "Test <t@example.com>|Test <t@example.com>"
    )


@pytest.mark.usefixtures("no_host_identity")
async def test_settings_fill_only_the_missing_email(repo: Path) -> None:
    unset_repo_identity(repo, "user.email")
    proj = make_changes(repo)
    await SPECS["git_commit"].handler(
        identity_ctx(proj, "Copper Bot", "bot@example.org"), {"message": "x"}
    )
    assert git(repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>").strip() == (
        "Test <bot@example.org>|Test <bot@example.org>"
    )


@pytest.mark.usefixtures("no_host_identity")
async def test_only_name_configured_still_reports_missing_email(repo: Path) -> None:
    unset_repo_identity(repo, "user.name", "user.email")
    proj = make_changes(repo)
    with pytest.raises(StructuredToolError) as err:
        await SPECS["git_commit"].handler(identity_ctx(proj, "Copper Bot", None), {"message": "x"})
    assert "COPPERCHAT_GIT_EMAIL" in err.value.to_dict()["suggestion"]


@pytest.mark.parametrize(
    ("name", "email"),
    [("a\nb", None), (None, "a@b\nc"), ("x\x00y", None), (None, "no-at-sign"), ("A <b>", None)],
)
def test_identity_settings_reject_bad_values(name: str | None, email: str | None) -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, git_name=name, git_email=email)  # type: ignore[call-arg]


def test_identity_settings_strip_and_empty_is_unset() -> None:
    s = Settings(_env_file=None, git_name="  Bot  ", git_email="   ")  # type: ignore[call-arg]
    assert s.git_name == "Bot" and s.git_email is None


def test_identity_settings_read_the_copperchat_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COPPERCHAT_GIT_NAME", "Env Bot")
    monkeypatch.setenv("COPPERCHAT_GIT_EMAIL", "env@example.org")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (s.git_name, s.git_email) == ("Env Bot", "env@example.org")
