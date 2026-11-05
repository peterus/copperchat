"""Agent git tools. Never push, never rewrite history, commit only on explicit request.

Every call is `git -C <project dir>` limited to the project directory minus
CopperChat's caches and KiCad's lock files; git never prompts and runs no hooks
or fsmonitor.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from copperchat.config import Settings
from copperchat.gitproc import GitNotFoundError, GitTimeoutError, run_git
from copperchat.tools import StructuredToolError, ToolContext, ToolResult, ToolSpec

# The current user message must ask for a commit: English (commit, committing) and
# German (committe, committen, committet, commite, commiten, commitet) forms. "committed" is
# deliberately not accepted ("I'm committed to this design"), nor is the noun "commits"
# ("show the commits"), nor "recommit", "uncommitted" or "committee".
COMMIT_INTENT = re.compile(r"\bcommit(?:ting|te|ten|tet|e|en|et)?\b", re.IGNORECASE)
EXCLUDED_DIRS = (
    ".kicad-agent/history",
    ".kicad-agent/view",
    ".kicad-agent/renders",
    ".kicad-agent/uploads",
    ".kicad-agent/images",
)
# KiCad's (and CopperChat's own api-server's) lock files are never project content.
_EXCLUDED_GLOBS = ("**/~*.lck",)
_TIMEOUT_S = 30.0
MESSAGE_MAX = 2000
# What git prints (LANG=C) when no committer identity can be determined.
_IDENTITY_ERRORS = (
    "author identity unknown",
    "committer identity unknown",
    "unable to auto-detect email address",
    "unable to auto-detect name",
    "please tell me who you are",
    "empty ident name",
)
_IDENTITY_FIX = (
    "Ask the user to set COPPERCHAT_GIT_NAME and COPPERCHAT_GIT_EMAIL in the CopperChat .env "
    "(and restart it) or to set user.name and user.email in the repository."
)


FILES_MAX = 200  # changed files listed per call (tool result and Git panel)
_DUBIOUS = "dubious ownership"


async def _git(project_dir: Path, *args: str) -> tuple[int, str, str]:
    try:
        code, out, err = await run_git(project_dir, *args, timeout_s=_TIMEOUT_S)
    except GitNotFoundError as exc:
        raise StructuredToolError(
            "git is not installed", str(exc), "Install git in the CopperChat image."
        ) from exc
    except GitTimeoutError as exc:
        raise StructuredToolError(
            f"git {args[0] if args else ''} timed out".strip(),
            f"git did not finish within {_TIMEOUT_S:g}s",
            "The repository may be very large or locked; try again later.",
        ) from exc
    return code, out.decode(errors="replace"), err.decode(errors="replace")


async def _pathspec(project_dir: Path) -> list[str]:
    """Pathspec for the project directory minus CopperChat caches and lock files.

    `git add -A` fails (exit 1, after staging the rest) when an exclude pathspec names a
    path that .gitignore already ignores, so excludes for ignored directories are
    dropped: git skips them anyway. Directories that are not ignored stay excluded.
    """
    code, out, _ = await _git(project_dir, "check-ignore", "--", *(f"{d}/" for d in EXCLUDED_DIRS))
    ignored = {line.rstrip("/") for line in out.splitlines()} if code == 0 else set()
    return [
        "--",
        ".",
        *(f":(exclude){d}" for d in EXCLUDED_DIRS if d not in ignored),
        *(f":(exclude,glob){g}" for g in _EXCLUDED_GLOBS),
    ]


async def _repo_state(project_dir: Path) -> tuple[bool, str]:
    code, out, err = await _git(project_dir, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out.strip() == "true", err.strip()


async def _require_repo(project_dir: Path) -> None:
    ok, err = await _repo_state(project_dir)
    if ok:
        return
    if _DUBIOUS in err:
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
        project_dir,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        *await _pathspec(project_dir),
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
    ok, err = await _repo_state(project_dir)
    if not ok:
        data: dict[str, Any] = {"isRepo": False, "branch": None, "clean": True, "files": []}
        if _DUBIOUS in err:
            data["error"] = (
                "git refuses the repository: it belongs to another user (dubious ownership). "
                "Make the projects mount owned by the CopperChat user (uid 1000)."
            )
        return data
    files = await _status_files(project_dir)
    return {
        "isRepo": True,
        "branch": await _branch(project_dir),
        "clean": not files,
        "files": files[:FILES_MAX],
        "total": len(files),
        "truncated": len(files) > FILES_MAX,
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
        _, out, _ = await _git(
            d, "diff", "HEAD", "--no-ext-diff", "--numstat", "-z", *await _pathspec(d)
        )
        numstat = _parse_numstat(out)
    status = await _status_files(d)
    files = []
    for f in status[:FILES_MAX]:
        added, deleted = numstat.get(f["path"], (None, None))
        files.append({"path": f["path"], "status": f["status"], "added": added, "deleted": deleted})
    return ToolResult(
        data={"files": files, "count": len(status), "truncated": len(status) > FILES_MAX}
    )


async def _identity_args(project_dir: Path, settings: Settings | None) -> list[str]:
    """``-c user.*`` overrides for the identity git itself does not know.

    Identity configured in git (repo-local or any config git sees) wins; the CopperChat
    settings only fill what is missing. ``-c`` sets author and committer alike, unlike
    GIT_AUTHOR_*/GIT_COMMITTER_* which would override the repository's own config.
    """
    if settings is None:
        return []
    args: list[str] = []
    for key, fallback in (("user.name", settings.git_name), ("user.email", settings.git_email)):
        if not fallback:
            continue
        code, out, _ = await _git(project_dir, "config", "--get", key)
        # Exit 1 = key not set. Any other failure (e.g. a corrupt config) is left for
        # `git commit` to report instead of papering over it with the fallback.
        if code in (0, 1) and not out.strip():
            args += ["-c", f"{key}={fallback}"]
    return args


async def git_commit(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    d = ctx.project.dir
    if not ctx.user_message or not COMMIT_INTENT.search(ctx.user_message):
        raise StructuredToolError(
            "Commit refused",
            "git_commit is only allowed when the user's current message explicitly asks to "
            "commit (earlier messages do not count)",
            "Ask the user whether they want to commit and wait for a message that says so, "
            "or tell them to use the Commit button.",
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
    pathspec = await _pathspec(d)
    code, _, err = await _git(d, "add", "-A", *pathspec)
    if code != 0:
        raise StructuredToolError("git add failed", err.strip(), "Check the repository state.")
    code, _, _ = await _git(d, "diff", "--cached", "--quiet", *pathspec)
    if code == 0:
        raise StructuredToolError(
            "Nothing to commit",
            "no project changes are left to commit after staging (the rest is git-ignored)",
            "Tell the user the working tree is clean.",
        )
    # With a pathspec, `git commit` records only those paths (--only): changes the
    # user staged outside the project directory stay staged and uncommitted.
    identity = await _identity_args(d, ctx.settings)
    code, _, err = await _git(d, *identity, "commit", "--no-verify", "-q", "-m", message, *pathspec)
    if code != 0:
        raise StructuredToolError(
            "git commit failed",
            err.strip(),
            _IDENTITY_FIX
            if any(m in err.lower() for m in _IDENTITY_ERRORS)
            else "Tell the user what git reported; they may need to finish a merge or "
            "remove a stale index.lock in their repository.",
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
        "explicitly asked to commit in their current message (not an earlier one). Never pushes.",
        {
            "type": "object",
            "properties": {"message": {"type": "string", "minLength": 1, "maxLength": MESSAGE_MAX}},
            "required": ["message"],
            "additionalProperties": False,
        },
        git_commit,
    ),
]
