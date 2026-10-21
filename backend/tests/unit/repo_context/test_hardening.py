"""Regression tests for the repo-context hardening findings (git dirs, names, FIFOs)."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest

from copperchat.agent.guards import repo_path_refusal
from copperchat.config import Settings
from copperchat.kicad_session import KicadSession
from copperchat.repo_context import specs
from copperchat.repo_context.listing import list_repo
from copperchat.repo_context.paths import (
    RepoPathError,
    RepoPolicy,
    check_rel_path,
    is_text_file,
    repo_policy,
    resolve_file,
)
from copperchat.tools import StructuredToolError, ToolContext

from .conftest import git, init_repo, make_project

TOOLS = {s.name: s for s in specs.TOOL_SPECS}


async def call(ctx: ToolContext, name: str, **args: Any) -> dict[str, Any]:
    result = await TOOLS[name].handler(ctx, args)
    assert result.data is not None
    return result.data


def _ctx(project_dir: Path, make_settings: Callable[..., Settings]) -> ToolContext:
    project = make_project(project_dir)
    return ToolContext(project=project, session=cast(KicadSession, None), settings=make_settings())


# --- 1. in-root git dir not named .git ----------------------------------------------------


@pytest.fixture
def bare_layout(tmp_path: Path) -> Path:
    root = tmp_path / "projects" / "repo"
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main", f"--separate-git-dir={root / '.bare'}", str(root))
    assert (root / ".git").is_file()
    (root / "README.md").write_text("# hi needle\n")
    (root / "cfg-link").symlink_to(root / ".bare" / "config")
    return root


async def test_git_dir_named_bare_is_blocked(
    bare_layout: Path, make_settings: Callable[..., Settings]
) -> None:
    root = bare_layout
    project = make_project(root / "hw")
    policy = await repo_policy(project, root.parent)
    assert policy.is_git
    assert PurePosixPath(".bare") in policy.git_dirs

    listed = [f.rel.as_posix() for f in (await list_repo(policy)).files]
    assert "README.md" in listed
    assert not [p for p in listed if p.startswith(".bare") or p == "cfg-link"]

    for raw in (".bare/config", ".BARE/config", "cfg-link"):
        with pytest.raises(RepoPathError):
            resolve_file(policy, raw)
        assert repo_path_refusal(policy, "repo_read", {"path": raw}) is not None
    assert repo_path_refusal(policy, "repo_list", {"path": ".bare"}) is not None

    ctx = _ctx(root / "hw", make_settings)
    with pytest.raises(StructuredToolError):
        await call(ctx, "repo_read", path=".bare/config")
    data = await call(ctx, "repo_grep", query="core")
    assert data["matches"] == []
    data = await call(ctx, "repo_grep", query="needle")
    assert [m["path"] for m in data["matches"]] == ["README.md"]


def test_policy_default_has_no_git_dirs(tmp_path: Path) -> None:
    policy = RepoPolicy(tmp_path, tmp_path, False, tmp_path.parent)
    assert policy.git_dirs == ()


# --- 2. non-UTF-8 file names --------------------------------------------------------------


def _bad_name(root: Path) -> None:
    try:
        with open(os.path.join(os.fsencode(root), b"Geh\xe4use.scad"), "wb") as fh:
            fh.write(b"cube(1);\n")
    except OSError:
        pytest.skip("the file system refuses non-UTF-8 names")


@pytest.mark.parametrize("is_git", [True, False])
async def test_non_utf8_names_skipped(tmp_path: Path, is_git: bool) -> None:
    root = tmp_path / "repo"
    if is_git:
        init_repo(root)
    else:
        root.mkdir()
    (root / "ok.scad").write_text("x\n")
    _bad_name(root)
    policy = RepoPolicy(root.resolve(), root.resolve(), is_git, tmp_path.resolve())
    files = (await list_repo(policy)).files
    assert [f.rel.as_posix() for f in files] == ["ok.scad"]
    for f in files:
        f.rel.as_posix().encode("utf-8")


# --- 3. gitignored files by exact path ----------------------------------------------------


async def test_read_refuses_gitignored(
    tmp_path: Path, make_settings: Callable[..., Settings]
) -> None:
    root = init_repo(tmp_path / "projects" / "repo")
    (root / ".gitignore").write_text("local.cfg\nignored-dir/\n")
    (root / "local.cfg").write_text("password=x\n")
    (root / "tracked.cfg").write_text("ok\n")
    (root / "ignored-dir").mkdir()
    (root / "ignored-dir" / "a.txt").write_text("a\n")
    (root / "alias.txt").symlink_to(root / "local.cfg")
    git(root, "add", ".gitignore", "tracked.cfg")
    git(root, "commit", "-q", "-m", "init")
    ctx = _ctx(root / "hw", make_settings)
    for raw in ("local.cfg", "ignored-dir/a.txt", "alias.txt"):
        with pytest.raises(StructuredToolError) as err:
            await call(ctx, "repo_read", path=raw)
        assert "ignore" in str(err.value).lower() or "ignore" in err.value.suggestion.lower()
    data = await call(ctx, "repo_read", path="tracked.cfg")
    assert data["content"] == "ok\n"


async def test_read_force_tracked_ignored_file_allowed(
    tmp_path: Path, make_settings: Callable[..., Settings]
) -> None:
    root = init_repo(tmp_path / "projects" / "repo")
    (root / ".gitignore").write_text("*.cfg\n")
    (root / "forced.cfg").write_text("ok\n")
    git(root, "add", "-f", ".gitignore", "forced.cfg")
    git(root, "commit", "-q", "-m", "init")
    ctx = _ctx(root / "hw", make_settings)
    data = await call(ctx, "repo_read", path="forced.cfg")
    assert data["content"] == "ok\n"


async def test_read_gitignore_irrelevant_without_git(
    tmp_path: Path, make_settings: Callable[..., Settings]
) -> None:
    project_dir = tmp_path / "projects" / "plain"
    project_dir.mkdir(parents=True)
    (project_dir / ".gitignore").write_text("local.cfg\n")
    (project_dir / "local.cfg").write_text("v\n")
    ctx = _ctx(project_dir, make_settings)
    data = await call(ctx, "repo_read", path="local.cfg")
    assert data["content"] == "v\n"


# --- 4. lone surrogates in agent paths ----------------------------------------------------


def test_lone_surrogate_path_refused(tmp_path: Path) -> None:
    with pytest.raises(RepoPathError):
        check_rel_path("a\ud800.txt")
    policy = RepoPolicy(tmp_path, tmp_path, False, tmp_path.parent)
    for tool, key in (("repo_read", "path"), ("repo_list", "path"), ("repo_grep", "glob")):
        assert repo_path_refusal(policy, tool, {key: "a\ud800.txt"}) is not None


# --- 5. FIFOs never block -----------------------------------------------------------------


async def _no_hang(fn: Callable[[], Any], fifo: Path) -> Any:
    task = asyncio.ensure_future(asyncio.to_thread(fn))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=3)
    except TimeoutError:
        # Unblock the stuck open so the thread can finish, then fail.
        with contextlib.suppress(OSError):
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        with contextlib.suppress(BaseException):
            await task
        pytest.fail("opening a FIFO blocked")


@pytest.fixture
def fifo(tmp_path: Path) -> Path:
    if not hasattr(os, "mkfifo"):
        pytest.skip("no FIFOs on this platform")
    path = tmp_path / "pipe.txt"
    os.mkfifo(path)
    return path


async def test_is_text_file_fifo(fifo: Path) -> None:
    assert await _no_hang(lambda: is_text_file(fifo), fifo) is False


async def test_read_lines_fifo(fifo: Path) -> None:
    def read() -> str:
        try:
            specs._read_lines(fifo, "pipe.txt")
        except (OSError, StructuredToolError) as exc:
            return type(exc).__name__
        return "read"

    assert await _no_hang(read, fifo) != "read"


async def test_repo_read_fifo_structured(
    tmp_path: Path, make_settings: Callable[..., Settings]
) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("no FIFOs on this platform")
    project_dir = tmp_path / "projects" / "plain"
    project_dir.mkdir(parents=True)
    os.mkfifo(project_dir / "pipe.txt")
    ctx = _ctx(project_dir, make_settings)
    with pytest.raises(StructuredToolError):
        await asyncio.wait_for(call(ctx, "repo_read", path="pipe.txt"), timeout=3)
