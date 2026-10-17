"""Agent git tools. Never push, never rewrite history, commit only on explicit request.

Every call is `git -C <project dir>` limited to the project directory minus
CopperChat's caches and KiCad's lock files; git never prompts and runs no hooks
or fsmonitor.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from pathlib import Path
from typing import Any

from copperchat.tools import StructuredToolError, ToolContext, ToolResult, ToolSpec

COMMIT_INTENT = re.compile(r"\bcommit\b", re.IGNORECASE)
EXCLUDED_DIRS = (
    ".kicad-agent/history",
    ".kicad-agent/view",
    ".kicad-agent/renders",
    ".kicad-agent/uploads",
    ".kicad-agent/images",
)
# KiCad's (and CopperChat's own api-server's) lock files are never project content.
_EXCLUDED_GLOBS = ("**/~*.lck",)
PATHSPEC = [
    "--",
    ".",
    *(f":(exclude){d}" for d in EXCLUDED_DIRS),
    *(f":(exclude,glob){g}" for g in _EXCLUDED_GLOBS),
]
_GIT_CONFIG = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")
_TIMEOUT_S = 30.0
MESSAGE_MAX = 2000


def _env() -> dict[str, str]:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LANG": "C", "LC_ALL": "C"}


async def _git(project_dir: Path, *args: str) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *_GIT_CONFIG,
            "-C",
            str(project_dir),
            *args,
            env=_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise StructuredToolError(
            "git is not installed", str(exc), "Install git in the CopperChat image."
        ) from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_S)
    except (TimeoutError, asyncio.CancelledError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def _repo_state(project_dir: Path) -> tuple[bool, str]:
    code, out, err = await _git(project_dir, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out.strip() == "true", err.strip()


async def _require_repo(project_dir: Path) -> None:
    ok, err = await _repo_state(project_dir)
    if ok:
        return
    if "dubious ownership" in err:
        raise StructuredToolError(
            "git refuses to work in the project's repository",
            "the repository belongs to another user (git 'dubious ownership')",
            "Tell the user to make the projects mount owned by the CopperChat user (uid 1000).",
        )
    raise StructuredToolError(
        "The project is not inside a git repository",
        f"{project_dir.name} has no enclosing .git directory",
        "Tell the user to run `git init` in the project folder if they want version control.",
    )


async def _branch(project_dir: Path) -> str | None:
    code, out, _ = await _git(project_dir, "symbolic-ref", "--quiet", "--short", "HEAD")
    return out.strip() if code == 0 else None


async def _status_files(project_dir: Path) -> list[dict[str, str]]:
    code, out, err = await _git(
        project_dir, "status", "--porcelain=v1", "-z", "--untracked-files=all", *PATHSPEC
    )
    if code != 0:
        raise StructuredToolError("git status failed", err.strip(), "Check the repository state.")
    files: list[dict[str, str]] = []
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            i += 1  # -z: the rename/copy source follows as its own entry
        files.append({"path": path, "status": status.strip()})
    return files


async def git_status_data(project_dir: Path) -> dict[str, Any]:
    """Status for the Git panel and the git_status tool; paths relative to the repo root."""
    if not (await _repo_state(project_dir))[0]:
        return {"isRepo": False, "branch": None, "clean": True, "files": []}
    files = await _status_files(project_dir)
    return {
        "isRepo": True,
        "branch": await _branch(project_dir),
        "clean": not files,
        "files": files,
    }


async def git_status(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    await _require_repo(ctx.project.dir)
    return ToolResult(data=await git_status_data(ctx.project.dir))


def _parse_numstat(out: str) -> dict[str, tuple[int | None, int | None]]:
    stats: dict[str, tuple[int | None, int | None]] = {}
    records = out.split("\0")
    i = 0
    while i < len(records):
        record = records[i]
        i += 1
        parts = record.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        if not path:  # -z rename: "added\tdeleted\t\0src\0dst"
            path = records[i + 1] if i + 1 < len(records) else ""
            i += 2
        stats[path] = (
            None if added == "-" else int(added),
            None if deleted == "-" else int(deleted),
        )
    return stats


async def git_diff_summary(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    d = ctx.project.dir
    await _require_repo(d)
    code, _, _ = await _git(d, "rev-parse", "--verify", "--quiet", "HEAD")
    numstat: dict[str, tuple[int | None, int | None]] = {}
    if code == 0:
        _, out, _ = await _git(d, "diff", "HEAD", "--no-ext-diff", "--numstat", "-z", *PATHSPEC)
        numstat = _parse_numstat(out)
    files = []
    for f in await _status_files(d):
        added, deleted = numstat.get(f["path"], (None, None))
        files.append({"path": f["path"], "status": f["status"], "added": added, "deleted": deleted})
    return ToolResult(data={"files": files, "count": len(files)})


async def git_commit(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    d = ctx.project.dir
    if not ctx.user_message or not COMMIT_INTENT.search(ctx.user_message):
        raise StructuredToolError(
            "Commit refused",
            "git_commit is only allowed when the user's message explicitly asks to commit",
            "Ask the user whether they want to commit, or tell them to use the Commit button.",
        )
    message = str(args.get("message", "")).strip()
    if not message or len(message) > MESSAGE_MAX:
        raise StructuredToolError(
            "Invalid commit message",
            f"message must be 1-{MESSAGE_MAX} characters",
            "Write a short summary of the design change.",
        )
    await _require_repo(d)
    if not await _status_files(d):
        raise StructuredToolError(
            "Nothing to commit",
            "the project has no changes",
            "Tell the user the working tree is clean.",
        )
    code, _, err = await _git(d, "add", "-A", *PATHSPEC)
    if code != 0:
        raise StructuredToolError("git add failed", err.strip(), "Check the repository state.")
    # With a pathspec, `git commit` records only those paths (--only): changes the
    # user staged outside the project directory stay staged and uncommitted.
    code, _, err = await _git(d, "commit", "--no-verify", "-q", "-m", message, *PATHSPEC)
    if code != 0:
        raise StructuredToolError(
            "git commit failed",
            err.strip(),
            "Ask the user to configure git user.name/user.email.",
        )
    _, sha, _ = await _git(d, "rev-parse", "HEAD")
    _, names, _ = await _git(d, "show", "--name-only", "-z", "--format=", "HEAD")
    return ToolResult(data={"commit": sha.strip(), "files": [n for n in names.split("\0") if n]})


_EMPTY: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        "git_status",
        "Branch and changed files of the project in its git repository (paths relative to "
        "the repository root; CopperChat caches and KiCad lock files excluded).",
        _EMPTY,
        git_status,
    ),
    ToolSpec(
        "git_diff_summary",
        "Changed project files with added/deleted line counts against HEAD "
        "(null counts for untracked or binary files).",
        _EMPTY,
        git_diff_summary,
    ),
    ToolSpec(
        "git_commit",
        "Commit the project's changes (project directory only). Only when the user "
        "explicitly asked to commit in their message. Never pushes.",
        {
            "type": "object",
            "properties": {"message": {"type": "string", "minLength": 1, "maxLength": MESSAGE_MAX}},
            "required": ["message"],
            "additionalProperties": False,
        },
        git_commit,
    ),
]
