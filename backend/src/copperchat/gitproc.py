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


async def run_git_capped(
    cwd: Path, *args: str, timeout_s: float, max_output_bytes: int
) -> tuple[int, bytes, bytes, bool]:
    """`run_git` with stdout bounded to ``max_output_bytes``.

    Returns ``(returncode, stdout, stderr, capped)``. Once stdout would exceed the cap,
    git is killed and ``capped`` is True: ``stdout`` then holds the first
    ``max_output_bytes`` bytes and the return code is not meaningful. stderr is kept
    to its first 64 KiB.
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
    assert proc.stdout is not None and proc.stderr is not None
    stdout, stderr = proc.stdout, proc.stderr

    async def read_out() -> tuple[bytes, bool]:
        buf = bytearray()
        while chunk := await stdout.read(65536):
            buf += chunk
            if len(buf) > max_output_bytes:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                return bytes(buf[:max_output_bytes]), True
        return bytes(buf), False

    async def read_err() -> bytes:
        buf = bytearray()
        while chunk := await stderr.read(65536):
            if len(buf) < 65536:
                buf += chunk
        return bytes(buf[:65536])

    try:
        async with asyncio.timeout(timeout_s):
            (out, capped), err = await asyncio.gather(read_out(), read_err())
            await proc.wait()
    except (TimeoutError, asyncio.CancelledError) as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise GitTimeoutError(f"git did not finish within {timeout_s:g}s") from exc
    return proc.returncode or 0, out, err, capped
