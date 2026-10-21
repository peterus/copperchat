"""Path policy for the agent's read-only repository access (spec §4).

Every agent-supplied path is checked statically, then resolved strictly inside
the repository root and checked against the block list on both the path as
given and the resolved path, so symlinks cannot reach secrets or leave the root.
"""

from __future__ import annotations

import codecs
import errno
import logging
import os
import stat
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from copperchat.gitproc import GitError, GitNotFoundError, GitTimeoutError, run_git
from copperchat.projects import Project

log = logging.getLogger(__name__)

MAX_PATH_LEN = 400
ROOT_LOOKUP_TIMEOUT_S = 5.0
SNIFF_BYTES = 8 * 1024

BLOCK_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("VCS internals", (".git",)),
    ("environment file", (".env*", "*.env")),
    (
        "secret or credential file",
        (
            "id_rsa*",
            "id_ed25519*",
            "id_ecdsa*",
            "id_dsa*",
            "*.pem",
            "*.key",
            "*.p12",
            "*.pfx",
            "*.keystore",
            "*.jks",
            ".netrc",
            ".npmrc",
            ".pypirc",
            "*credentials*",
            "*secret*",
            "*.kdbx",
            # Beyond spec §4.3 (author's call, §12): common credential stores.
            ".ssh",
            ".gnupg",
            ".aws",
            ".pgpass",
            ".htpasswd",
            "*.tfstate",
            "*.tfstate.*",
            "*.tfvars",
        ),
    ),
    ("CopperChat state", (".kicad-agent",)),
    ("KiCad backup/cache/lock file", ("*-backups", "fp-info-cache", "*.lck")),
)
"""The block list (spec §4.3), one row per kind: (reason, lower-case glob patterns).

Every pattern is matched case-insensitively against **every** segment of a path
relative to the repository root, so a blocked directory hides everything below
it and a blocked file name is blocked wherever it sits. Blocked paths are
hidden from listings and grep and are never readable. KiCad design files are
not on this list: they are listed but not readable (`is_kicad_file`); binaries
are listed but not readable (`is_text_file`).
"""

KICAD_SUFFIXES = (
    ".kicad_sch",
    ".kicad_pcb",
    ".kicad_sym",
    ".kicad_mod",
    ".kicad_pro",
    ".kicad_prl",
    ".kicad_dru",
    ".kicad_wks",
)
KICAD_NAMES = ("sym-lib-table", "fp-lib-table")


class RepoPathError(Exception):
    """An agent-supplied repository path was refused."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


@dataclass(frozen=True)
class RepoPolicy:
    root: Path
    project_dir: Path
    is_git: bool
    projects_root: Path
    git_dirs: tuple[PurePosixPath, ...] = ()
    """Git directories inside the root under another name than ``.git`` (e.g. ``.bare``
    in a bare-repo worktree layout), relative to the root; blocked like ``.git``."""
    transient: str = ""
    """Set (a short reason) when the "no git" fallback was taken because git could not be
    run or refused the repository (not cached); the tools and the brief then refuse."""

    def blocked(self, rel: PurePosixPath) -> str | None:
        """`blocked_reason` plus this repository's own git directories."""
        reason = blocked_reason(rel)
        if reason is not None:
            return reason
        parts = tuple(part.lower() for part in rel.parts)
        for git_dir in self.git_dirs:
            prefix = tuple(part.lower() for part in git_dir.parts)
            if parts[: len(prefix)] == prefix:
                return "VCS internals"
        return None

    @property
    def rel_root(self) -> str:
        """The repository root relative to the projects root (POSIX)."""
        try:
            return self.root.relative_to(self.projects_root).as_posix()
        except ValueError:
            return self.root.as_posix()

    @property
    def rel_project_dir(self) -> str:
        """The project directory relative to the repository root (``"."`` if equal)."""
        try:
            return self.project_dir.relative_to(self.root).as_posix()
        except ValueError:
            return "."


_policy_cache: dict[tuple[Path, Path], RepoPolicy] = {}


def clear_repo_policy_cache() -> None:
    _policy_cache.clear()


GIT_MISSING = "git is not installed"
GIT_TIMEOUT = "git timed out"
GIT_DUBIOUS = "git refuses the repository: it belongs to another user (dubious ownership)"
GIT_FAILED = "git failed"


class _GitUnavailable(Exception):
    """The git top-level lookup failed (git missing, timeout, refused): do not cache.
    ``reason`` is one of the short fixed ``GIT_*`` texts."""

    def __init__(self, message: str, reason: str = GIT_FAILED) -> None:
        super().__init__(message)
        self.reason = reason


def _error_reason(exc: GitError) -> str:
    if isinstance(exc, GitNotFoundError):
        return GIT_MISSING
    if isinstance(exc, GitTimeoutError):
        return GIT_TIMEOUT
    return GIT_FAILED


def _exit_reason(message: str) -> str:
    return GIT_DUBIOUS if "dubious ownership" in message.lower() else GIT_FAILED


async def _git_toplevel(project_dir: Path) -> Path | None:
    """The git top-level of `project_dir`, or None if git says it is not in a repo.

    Raises `_GitUnavailable` if git could not be run to completion or failed for any
    other reason (e.g. "dubious ownership"), so such a failure is not remembered as
    "no git".
    """
    try:
        code, out, err = await run_git(
            project_dir, "rev-parse", "--show-toplevel", timeout_s=ROOT_LOOKUP_TIMEOUT_S
        )
    except GitError as exc:
        raise _GitUnavailable(str(exc) or type(exc).__name__, _error_reason(exc)) from exc
    if code != 0:
        message = os.fsdecode(err).strip()
        if "not a git repository" in message.lower():
            return None
        raise _GitUnavailable(
            message.splitlines()[0] if message else f"exit code {code}", _exit_reason(message)
        )
    text = os.fsdecode(out).strip()
    if not text:
        return None
    try:
        return Path(text).resolve(strict=True)
    except (OSError, RuntimeError):
        return None


async def _git_dirs_inside(project_dir: Path, root: Path) -> tuple[PurePosixPath, ...]:
    """The git dir and common dir of `project_dir` that lie inside `root`, relative to it.

    Raises `_GitUnavailable` if git could not tell (so the policy is not cached).
    """
    try:
        code, out, err = await run_git(
            project_dir,
            "rev-parse",
            "--absolute-git-dir",
            "--git-common-dir",
            timeout_s=ROOT_LOOKUP_TIMEOUT_S,
        )
    except GitError as exc:
        raise _GitUnavailable(str(exc) or type(exc).__name__, _error_reason(exc)) from exc
    lines = [line for line in os.fsdecode(out).splitlines() if line.strip()]
    if code != 0 or len(lines) != 2:
        message = os.fsdecode(err).strip()
        raise _GitUnavailable(
            message.splitlines()[0] if message else f"exit code {code}", _exit_reason(message)
        )
    found: list[PurePosixPath] = []
    for line in lines:
        try:
            resolved = (project_dir / line.strip()).resolve(strict=True)
            rel = PurePosixPath(resolved.relative_to(root).as_posix())
        except (OSError, RuntimeError, ValueError):
            continue
        if rel.parts and rel not in found:
            found.append(rel)
    return tuple(found)


GITFILE_MAX_BYTES = 4096


def _read_small(path: Path) -> str | None:
    """The text of the small regular file `path` (no symlink followed), or None."""
    try:
        with os.fdopen(open_regular(path), "rb") as fh:
            data = fh.read(GITFILE_MAX_BYTES)
    except (OSError, RuntimeError):
        return None
    return data.decode("utf-8", errors="replace")


def _inside(root: Path, base: Path, raw: str) -> PurePosixPath | None:
    """`raw` (absolute, or relative to `base`) resolved, relative to `root` if strictly inside."""
    raw = raw.strip()
    if not raw or "\x00" in raw:
        return None
    try:
        resolved = (base / raw).resolve(strict=True)
        rel = PurePosixPath(resolved.relative_to(root).as_posix())
    except (OSError, RuntimeError, ValueError):
        return None
    return rel if rel.parts else None


def gitfile_dirs(root: Path) -> tuple[PurePosixPath, ...]:
    """Git dirs named by a ``<root>/.git`` *file* (``gitdir: <path>``, e.g. after
    ``--separate-git-dir``) and its ``commondir``, if they lie inside `root`.

    Read without git, so they are blocked even when git cannot be run. Unreadable or
    malformed content yields nothing.
    """
    text = _read_small(root / ".git")
    if text is None:
        return ()
    first = text.splitlines()[0] if text else ""
    if not first.startswith("gitdir:"):
        return ()
    rel = _inside(root, root, first[len("gitdir:") :])
    if rel is None:
        return ()
    found = [rel]
    common = _read_small(root / rel / "commondir")
    if common:
        common_rel = _inside(root, root / rel, common.splitlines()[0])
        if common_rel is not None and common_rel != rel:
            found.append(common_rel)
    return tuple(found)


def looks_like_git_dir(path: Path) -> bool:
    """True if `path` is a real directory (not a symlink) shaped like a git dir: a regular
    file ``HEAD`` and directories ``objects`` and ``refs``."""
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            return False
        return (
            stat.S_ISREG(os.lstat(path / "HEAD").st_mode)
            and stat.S_ISDIR(os.lstat(path / "objects").st_mode)
            and stat.S_ISDIR(os.lstat(path / "refs").st_mode)
        )
    except (OSError, ValueError):
        return False


def in_git_shaped_dir(root: Path, rel: PurePosixPath) -> bool:
    """True if `rel` or one of its ancestors below `root` looks like a git dir."""
    parts = rel.parts
    return any(looks_like_git_dir(root.joinpath(*parts[:n])) for n in range(1, len(parts) + 1))


def _merge(*groups: tuple[PurePosixPath, ...]) -> tuple[PurePosixPath, ...]:
    return tuple(dict.fromkeys(d for group in groups for d in group))


async def repo_policy(project: Project, projects_root: Path) -> RepoPolicy:
    """The repository boundary for `project` (spec §4.1), cached per project directory.

    The git top-level is used only if it is the project directory or one of its
    ancestors and lies strictly inside the projects root; otherwise the root is
    the project directory itself and the repo counts as "no git". If git could not be
    run (missing, timed out) or failed other than with "not a git repository", the
    "no git" fallback is used but not cached.
    """
    project_dir = project.dir.resolve()
    root_dir = projects_root.resolve()
    key = (project_dir, root_dir)
    cached = _policy_cache.get(key)
    if cached is not None:
        return cached
    try:
        top = await _git_toplevel(project_dir)
    except _GitUnavailable as exc:
        log.warning(
            "git top-level lookup failed for %s, using the project dir: %s", project_dir, exc
        )
        return RepoPolicy(
            project_dir,
            project_dir,
            False,
            root_dir,
            gitfile_dirs(project_dir),
            transient=exc.reason,
        )
    usable = (
        top is not None
        and (top == project_dir or top in project_dir.parents)
        and root_dir in top.parents
    )
    if usable and top is not None:
        try:
            git_dirs = await _git_dirs_inside(project_dir, top)
        except _GitUnavailable as exc:
            log.warning("git dir lookup failed for %s, using the project dir: %s", project_dir, exc)
            return RepoPolicy(
                project_dir,
                project_dir,
                False,
                root_dir,
                gitfile_dirs(project_dir),
                transient=exc.reason,
            )
        policy = RepoPolicy(top, project_dir, True, root_dir, _merge(git_dirs, gitfile_dirs(top)))
    else:
        policy = RepoPolicy(project_dir, project_dir, False, root_dir, gitfile_dirs(project_dir))
    _policy_cache[key] = policy
    return policy


def check_rel_path(raw: str) -> PurePosixPath:
    """Static checks on an agent path (spec §4.2); ``""``/``"."`` mean the root."""
    hint = "Pass a path relative to the repository root, e.g. docs/README.md."
    if len(raw) > MAX_PATH_LEN:
        raise RepoPathError(f"Path is longer than {MAX_PATH_LEN} characters.", hint)
    if any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise RepoPathError("Path contains control characters.", hint)
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        raise RepoPathError("Path is not valid UTF-8 text.", hint) from None
    if raw.startswith("/"):
        raise RepoPathError("Absolute paths are not allowed.", hint)
    if raw.startswith("~"):
        raise RepoPathError("Home-directory paths (~) are not allowed.", hint)
    if "\\" in raw:
        raise RepoPathError("Backslashes are not allowed; use forward slashes.", hint)
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise RepoPathError("'..' segments are not allowed.", hint)
    return PurePosixPath(*parts) if parts else PurePosixPath(".")


def blocked_reason(rel: PurePosixPath) -> str | None:
    """Why `rel` (relative to the root) is blocked, or None (spec §4.3 non-KiCad rows)."""
    for segment in rel.parts:
        name = segment.lower()
        for reason, patterns in BLOCK_RULES:
            if any(fnmatchcase(name, pattern) for pattern in patterns):
                return reason
    return None


def is_kicad_file(rel: PurePosixPath) -> bool:
    name = rel.name.lower()
    return name in KICAD_NAMES or name.endswith(KICAD_SUFFIXES)


def _blocked_error(reason: str) -> RepoPathError:
    return RepoPathError(
        f"Access denied: {reason}.", "This path is protected and cannot be listed or read."
    )


def _resolve(policy: RepoPolicy, raw: str) -> tuple[Path, PurePosixPath]:
    rel = check_rel_path(raw)
    reason = policy.blocked(rel)
    if reason is not None:
        raise _blocked_error(reason)
    try:
        resolved = (policy.root / rel).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RepoPathError(
            f"{rel.as_posix()} does not exist.", "Use repo_list to see the available files."
        ) from exc
    try:
        resolved_rel = PurePosixPath(resolved.relative_to(policy.root).as_posix())
    except ValueError as exc:
        raise RepoPathError(
            f"{rel.as_posix()} points outside the repository.",
            "Only files inside the repository can be accessed.",
        ) from exc
    reason = policy.blocked(resolved_rel)
    if reason is None and (
        in_git_shaped_dir(policy.root, rel) or in_git_shaped_dir(policy.root, resolved_rel)
    ):
        reason = "VCS internals"
    if reason is not None:
        raise _blocked_error(reason)
    return resolved, rel


def resolve_file(policy: RepoPolicy, raw: str) -> tuple[Path, PurePosixPath]:
    """Resolve an agent file path to an existing regular file inside the root."""
    resolved, rel = _resolve(policy, raw)
    if not resolved.is_file():
        raise RepoPathError(
            f"{rel.as_posix()} is not a file.", "Use repo_list to see the files in a directory."
        )
    return resolved, rel


def resolve_dir(policy: RepoPolicy, raw: str) -> tuple[Path, PurePosixPath]:
    """Resolve an agent directory path to an existing directory inside the root."""
    resolved, rel = _resolve(policy, raw)
    if not resolved.is_dir():
        raise RepoPathError(
            f"{rel.as_posix()} is not a directory.", "Pass a directory relative to the root."
        )
    return resolved, rel


def is_text_bytes(data: bytes) -> bool:
    """True if the first 8 KiB of `data` have no NUL and decode as UTF-8 (spec §4.5).

    A multi-byte sequence cut at the 8 KiB edge is tolerated.
    """
    chunk = data[:SNIFF_BYTES]
    if b"\x00" in chunk:
        return False
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        decoder.decode(chunk, final=len(chunk) < SNIFF_BYTES)
    except UnicodeDecodeError:
        return False
    return True


_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


def open_regular(path: Path) -> int:
    """A read-only fd on the regular file `path` (already resolved: no symlink followed).

    ``O_NONBLOCK`` keeps a FIFO or device swapped in after the checks from blocking the
    open; the regular-file check then runs on the opened fd. Raises `OSError` otherwise.
    """
    fd = os.open(path, _OPEN_FLAGS)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
    except BaseException:
        os.close(fd)
        raise
    return fd


def is_text_file(path: Path) -> bool:
    """`is_text_bytes` on the first 8 KiB of the regular file `path` (False if it cannot
    be read or is not a regular file)."""
    try:
        with os.fdopen(open_regular(path.resolve(strict=True)), "rb") as fh:
            chunk = fh.read(SNIFF_BYTES)
    except (OSError, RuntimeError):
        return False
    return is_text_bytes(chunk)
