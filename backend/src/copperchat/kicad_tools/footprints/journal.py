"""The footprint write journal of one tool call (FP-C7, spec §9.3): thin helpers over
`FootprintWriteRepo`. A row is written `pending` before its stage starts and finalised
(`done` / `compensated` / `leftover`) with the file's hash afterwards. Without an editing
service (plain tests, no run engine) nothing is journalled.

`file_rel` is repo-root-relative POSIX: the project's `fp-lib-table` for `row_add`
(`lib_id` = the nickname), the `.kicad_mod` for `create` / `replace`
(`lib_id` = `<nick>:<name>`)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from copperchat.db.repos.footprints import ABSENT, FootprintWrite, FootprintWriteRepo
from copperchat.errors import StructuredToolError
from copperchat.library.local import LocalLibrary
from copperchat.repo_context.paths import open_regular
from copperchat.tools.spec import ToolContext

__all__ = [
    "ABSENT",
    "Journal",
    "PendingOutcome",
    "classify_pending",
    "file_sha256",
    "footprint_rel",
    "repo_rel",
    "scrub_repo",
    "table_rel",
]

log = logging.getLogger(__name__)


def repo_rel(root: Path, path: Path) -> str:
    """`path` relative to the repo root `root` (POSIX), for agent-facing text; host paths
    stay in the logs."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:  # a project outside the repo root (never for repo_policy roots)
        return Path(os.path.relpath(path, root)).as_posix()


def scrub_repo(text: str, root: Path) -> str:
    """`text` (a KiCad or OS error message) with the repo root's host path replaced by
    `<repo>`, so agent-facing errors carry no absolute host paths."""
    for prefix in sorted({str(root), root.as_posix(), str(root.resolve())}, key=len, reverse=True):
        if prefix and prefix != ".":
            text = text.replace(prefix, "<repo>")
    return text


def file_sha256(path: Path, root: Path | None = None) -> str:
    """sha256 of the regular file `path` (no symlink followed), or `absent`; anything
    else there (a symlink, a FIFO, ...) is `library_conflict`. Its text and
    `details.path` name the file relative to the repo root `root` (only the file name
    without one); the host path goes to the log."""
    try:
        fd = open_regular(path)
    except FileNotFoundError:
        return ABSENT
    except OSError as exc:  # swapped for a symlink (ELOOP), a FIFO (EINVAL), ...
        log.warning("%s cannot be read as a regular file: %s", path, exc)
        shown = path.name if root is None else repo_rel(root, path)
        raise StructuredToolError(
            "The local footprint library is not safe to write",
            f"{shown} cannot be read as a regular file: {exc.strerror or type(exc).__name__}",
            "Ask the user to replace it with the footprint file KiCad saved; nothing was written.",
            code="library_conflict",
            details={"path": shown},
        ) from exc
    digest = hashlib.sha256()
    with os.fdopen(fd, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repo_rel(lib: LocalLibrary, path: Path) -> str:
    return repo_rel(lib.repo_root, path)


def table_rel(lib: LocalLibrary) -> str:
    """The project's `fp-lib-table`, repo-root-relative (the `row_add` file)."""
    return _repo_rel(lib, lib.table)


def footprint_rel(lib: LocalLibrary, name: str) -> str:
    """`<library dir>/<name>.kicad_mod`, repo-root-relative."""
    return _repo_rel(lib, lib.dir / f"{name}.kicad_mod")


class Journal:
    """The journal rows one footprint tool call writes (ids per row; no-op without an
    editing service)."""

    def __init__(self, ctx: ToolContext) -> None:
        self._repo = FootprintWriteRepo(ctx.editing.db) if ctx.editing is not None else None
        self._project_id = ctx.project.id
        self._run_id = ctx.run_id

    async def pending(
        self, op: str, lib_id: str, file_rel: str, pre_sha256: str, geometry_hash: str | None = None
    ) -> int | None:
        if self._repo is None:
            return None
        return await self._repo.insert_pending(
            self._project_id, self._run_id, lib_id, file_rel, op, pre_sha256, geometry_hash
        )

    async def post_s2(self, row: int | None, sha: str) -> None:
        if self._repo is not None and row is not None:
            await self._repo.set_post_s2(row, sha)

    async def finalise(self, row: int | None, state: str, post_sha256: str) -> None:
        if self._repo is not None and row is not None:
            await self._repo.finalise(row, state, post_sha256)


async def sha_of(path: Path, root: Path | None = None) -> str:
    """`file_sha256` in a worker thread."""
    return await asyncio.to_thread(file_sha256, path, root)


# -- pending rows after a server death (spec §9.3, FP-C8) ---------------------------------

PendingAction = Literal["delete", "leftover"]


@dataclass(frozen=True)
class PendingOutcome:
    """What the cleanup does with one `pending` journal row: `delete` (an untouched seed
    copy, removed with `DeleteLibraryItem`, the row becomes `compensated`) or
    `leftover` (kept, the row becomes `leftover`); `current_sha256` is the file's hash
    the decision was made on."""

    row: FootprintWrite
    action: PendingAction
    current_sha256: str


def classify_pending(
    rows: Iterable[FootprintWrite], current_sha: Callable[[str], str]
) -> list[PendingOutcome]:
    """Pure (spec §9.3 point 2; used by `prepare` read-only and by the cleanup): a
    `create` row whose file's current hash (`current_sha(row.file_rel)`) equals its
    `post_s2_sha256` is `delete`; everything else is `leftover` - a `replace`,
    `approve`, `delete` or `row_add` row, a file changed or gone since S2, and a
    missing hash. Only the hashes count (no server generation, no run)."""
    out: list[PendingOutcome] = []
    for row in rows:
        current = current_sha(row.file_rel)
        untouched = (
            row.op == "create"
            and row.post_s2_sha256 not in (None, ABSENT)
            and current == row.post_s2_sha256
        )
        out.append(PendingOutcome(row, "delete" if untouched else "leftover", current))
    return out
