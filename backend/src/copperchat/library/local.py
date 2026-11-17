"""The project's local footprint library: pure derivation of its nickname and paths.

No KiCad import. The library lives at `<repo_root>/footprints/<nickname>.pretty`; the
project row points at it with a `${KIPRJMOD}`-relative URI.
"""

from __future__ import annotations

import os
import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,99}\Z")
LIBRARY_DESCRIPTION = "CopperChat local footprints"
NICKNAME_MAX = 32
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


@dataclass(frozen=True)
class LocalLibrary:
    nickname: str
    dir: Path  # <repo_root>/footprints/<nickname>.pretty (may not exist yet)
    uri: str  # "${KIPRJMOD}/../../footprints/<nickname>.pretty" (POSIX)
    repo_root: Path
    table: Path  # <project_dir>/fp-lib-table (may not exist)


def base_nickname(repo_root: Path) -> str:
    """The repo directory name reduced to [A-Za-z0-9_-] (others -> "_"), at most 32 chars."""
    return _UNSAFE.sub("_", repo_root.name)[:NICKNAME_MAX]


def derive_local_library(
    project_dir: Path, repo_root: Path, global_nicknames: Collection[str]
) -> LocalLibrary:
    nickname = base_nickname(repo_root)
    if nickname in global_nicknames:
        nickname += "_local"
    directory = repo_root / "footprints" / f"{nickname}.pretty"
    rel = Path(os.path.relpath(directory, project_dir)).as_posix()
    return LocalLibrary(
        nickname=nickname,
        dir=directory,
        uri=f"${{KIPRJMOD}}/{rel}",
        repo_root=repo_root,
        table=project_dir / "fp-lib-table",
    )
