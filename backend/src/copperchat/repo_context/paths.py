"""Path policy for the agent's read-only repository access (spec §4).

Every agent-supplied path is checked statically, then resolved strictly inside
the repository root and checked against the block list on both the path as
given and the resolved path, so symlinks cannot reach secrets or leave the root.
"""

from __future__ import annotations

import codecs
import logging
import os
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from copperchat.gitproc import GitError, run_git
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


class _GitUnavailable(Exception):
    """The git top-level lookup failed transiently (git missing, timeout): do not cache."""


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
        raise _GitUnavailable(str(exc) or type(exc).__name__) from exc
    if code != 0:
        message = os.fsdecode(err).strip()
        if "not a git repository" in message.lower():
            return None
        raise _GitUnavailable(message.splitlines()[0] if message else f"exit code {code}")
    text = os.fsdecode(out).strip()
    if not text:
        return None
    try:
        return Path(text).resolve(strict=True)
    except (OSError, RuntimeError):
        return None


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
        return RepoPolicy(project_dir, project_dir, False, root_dir)
    usable = (
        top is not None
        and (top == project_dir or top in project_dir.parents)
        and root_dir in top.parents
    )
    if usable and top is not None:
        policy = RepoPolicy(top, project_dir, True, root_dir)
    else:
        policy = RepoPolicy(project_dir, project_dir, False, root_dir)
    _policy_cache[key] = policy
    return policy


def check_rel_path(raw: str) -> PurePosixPath:
    """Static checks on an agent path (spec §4.2); ``""``/``"."`` mean the root."""
    hint = "Pass a path relative to the repository root, e.g. docs/README.md."
    if len(raw) > MAX_PATH_LEN:
        raise RepoPathError(f"Path is longer than {MAX_PATH_LEN} characters.", hint)
    if any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise RepoPathError("Path contains control characters.", hint)
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
    reason = blocked_reason(rel)
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
    reason = blocked_reason(resolved_rel)
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


def is_text_file(path: Path) -> bool:
    """`is_text_bytes` on the first 8 KiB of `path` (False if it cannot be read)."""
    try:
        with path.open("rb") as fh:
            chunk = fh.read(SNIFF_BYTES)
    except OSError:
        return False
    return is_text_bytes(chunk)
