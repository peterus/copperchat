"""Hardened git subprocess helper shared by the git tools and the repo context.

git never prompts, runs no hooks or fsmonitor, ignores stray ``GIT_*`` variables
from the server environment and takes no optional locks. The caller chooses the
timeout; on timeout or cancellation the process is killed and reaped.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

GIT_CONFIG = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")


class GitError(Exception):
    """git could not be run to completion (not installed, timed out)."""


class GitNotFoundError(GitError):
    """The git executable is missing."""


class GitTimeoutError(GitError):
    """git did not finish within the caller's timeout."""


def git_env() -> dict[str, str]:
    # No stray GIT_DIR/GIT_WORK_TREE/... from the server environment; no optional locks,
    # so a status never takes index.lock away from the user's own git.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return {
        **env,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "LANG": "C",
        "LC_ALL": "C",
    }


async def run_git(cwd: Path, *args: str, timeout_s: float) -> tuple[int, bytes, bytes]:
    """Run ``git -C cwd args`` and return ``(returncode, stdout, stderr)``.

    Raises `GitNotFoundError` if git is missing and `GitTimeoutError` on timeout.
    A non-zero exit is not an error here; the caller decides.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *GIT_CONFIG,
            "-C",
            str(cwd),
            *args,
            env=git_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise GitNotFoundError(str(exc)) from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except (TimeoutError, asyncio.CancelledError) as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise GitTimeoutError(f"git did not finish within {timeout_s:g}s") from exc
    return proc.returncode or 0, out, err
