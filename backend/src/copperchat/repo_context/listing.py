"""The repository file list behind repo_list / repo_grep and the brief (spec §4.4).

Git repos are listed with ``git ls-files`` (respecting ``.gitignore``; if that fails
the listing fails with `ListingError` rather than walking ignored files); other
roots are walked without following directory symlinks. Every path is checked
against the block list, symlinks are kept only if they point at a regular file
inside the root that is not blocked, files below a symlinked directory are dropped, and
the result is capped and cached.
"""

from __future__ import annotations

import asyncio
import os
import stat
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from copperchat.gitproc import GitError, run_git_capped
from copperchat.repo_context.paths import RepoPolicy, blocked_reason

WALK_CAP = 20_000
CACHE_TTL_S = 10.0
LS_FILES_TIMEOUT_S = 10.0
LS_FILES_MAX_BYTES = 16 * 1024 * 1024  # far more than WALK_CAP entries need


class ListingError(Exception):
    """``git ls-files`` failed in a git repo; the listing is refused, not walked."""


@dataclass(frozen=True)
class RepoFile:
    rel: PurePosixPath
    size: int


@dataclass(frozen=True)
class Listing:
    files: tuple[RepoFile, ...]
    truncated: bool


_cache: dict[Path, tuple[float, Listing]] = {}


def clear_listing_cache() -> None:
    _cache.clear()


def _file_size(root: Path, rel: PurePosixPath) -> int | None:
    """Size of `rel` if it is a regular file (or a symlink to one inside the root, unblocked)."""
    full = root / rel
    try:
        st = full.lstat()
    except OSError:
        return None
    if stat.S_ISREG(st.st_mode):
        return st.st_size
    if not stat.S_ISLNK(st.st_mode):
        return None
    try:
        target = full.resolve(strict=True)
        target_rel = PurePosixPath(target.relative_to(root).as_posix())
        st = target.stat()
    except (OSError, RuntimeError, ValueError):
        return None
    if blocked_reason(target_rel) is not None or not stat.S_ISREG(st.st_mode):
        return None
    return st.st_size


def _real_parent(root: Path, parent: PurePosixPath, memo: dict[PurePosixPath, bool]) -> bool:
    """True if `parent` resolves to itself below `root` (no symlinked directory on the way).

    A tracked path whose directory was replaced by a symlink (to ``.git``, outside the
    root, or elsewhere inside it) must not be listed through that symlink.
    """
    known = memo.get(parent)
    if known is None:
        expected = root / parent
        try:
            known = expected.resolve(strict=True) == expected
        except (OSError, RuntimeError):
            known = False
        memo[parent] = known
    return known


def _collect(root: Path, candidates: Iterable[PurePosixPath]) -> Listing:
    files: list[RepoFile] = []
    truncated = False
    parents: dict[PurePosixPath, bool] = {}
    for rel in candidates:
        if blocked_reason(rel) is not None or not _real_parent(root, rel.parent, parents):
            continue
        size = _file_size(root, rel)
        if size is None:
            continue
        if len(files) >= WALK_CAP:
            truncated = True
            break
        files.append(RepoFile(rel, size))
    files.sort(key=lambda f: f.rel.parts)
    return Listing(tuple(files), truncated)


def _walk_candidates(root: Path) -> Iterable[PurePosixPath]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = PurePosixPath(Path(dirpath).relative_to(root).as_posix())
        # Prune blocked and symlinked directories in place, before descending.
        dirnames[:] = sorted(
            d
            for d in dirnames
            if blocked_reason(base / d) is None and not os.path.islink(os.path.join(dirpath, d))
        )
        for name in sorted(filenames):
            yield base / name


def _git_candidates(out: bytes) -> list[PurePosixPath]:
    seen: dict[str, None] = {}
    for raw in out.split(b"\0"):
        if raw:
            seen.setdefault(os.fsdecode(raw), None)
    return [PurePosixPath(p) for p in seen]


async def _git_ls_files(root: Path) -> tuple[bytes, bool]:
    """The NUL-separated ``ls-files`` output and whether it was cut at the byte cap.

    Raises `ListingError` if git could not be run or failed.
    """
    try:
        code, out, err, capped = await run_git_capped(
            root,
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            timeout_s=LS_FILES_TIMEOUT_S,
            max_output_bytes=LS_FILES_MAX_BYTES,
        )
    except GitError as exc:
        raise ListingError(str(exc) or type(exc).__name__) from exc
    if capped:
        # Drop the entry the cap cut in half.
        return out[: out.rfind(b"\0") + 1], True
    if code != 0:
        detail = os.fsdecode(err).strip().splitlines()
        raise ListingError(detail[0] if detail else f"git ls-files exited with code {code}")
    return out, False


async def list_repo(policy: RepoPolicy) -> Listing:
    """All listable files under the repository root, sorted by path (cached 10 s per root).

    Raises `ListingError` (not cached) if ``git ls-files`` fails in a git repo.
    """
    root = policy.root
    now = time.monotonic()
    cached = _cache.get(root)
    if cached is not None and now - cached[0] < CACHE_TTL_S:
        return cached[1]
    if policy.is_git:
        out, capped = await _git_ls_files(root)
        result = await asyncio.to_thread(lambda: _collect(root, _git_candidates(out)))
        if capped and not result.truncated:
            result = Listing(result.files, True)
    else:
        result = await asyncio.to_thread(lambda: _collect(root, _walk_candidates(root)))
    _cache[root] = (time.monotonic(), result)
    return result
