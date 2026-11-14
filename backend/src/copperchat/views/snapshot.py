"""KiCanvas sources: copies of the open documents as the api-server holds them (the
turn end's catch-up after live frames copies the schematic files on disk instead,
`copy_disk_schematic`, without KiCad).

Written to `<project>/.kicad-agent/view/` under the documents' real file names
(hierarchical sub-sheets keep their relative paths), so KiCanvas resolves sheet
references between them (X3). Writing a snapshot never bumps
`KicadSession.view_version`; only `HistoryService.publish_view` (X2: the turn-end,
external-change, restore and the debounced live schematic publishes) and
`HistoryService.publish_disk_view` (the turn end's catch-up after live frames) do;
their frames list the view files per doc as the directory holds them after the write
(`view_file_list`).
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import logging
import os
import stat
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from copperchat.fsutil import atomic_write_bytes
from copperchat.kicad_session import DocKind, KicadHandle, KicadSession
from copperchat.projects import Project, agent_dir

log = logging.getLogger(__name__)

VIEW_SUFFIXES: dict[DocKind, str] = {"sch": ".kicad_sch", "pcb": ".kicad_pcb"}


def view_dir(project: Project) -> Path:
    return agent_dir(project) / "view"


def snapshot_path(project: Project, doc: DocKind) -> Path:
    """The root document's snapshot (root sheet for `sch`)."""
    return view_dir(project) / f"{project.name}{VIEW_SUFFIXES[doc]}"


def _sheet_files(h: KicadHandle) -> list[str]:
    return [str(s.filename) for s in h.schematic().get_hierarchy() if str(s.filename)]


def _project_has(project: Project, doc: DocKind) -> bool:
    return (project.sch_file if doc == "sch" else project.pcb_file) is not None


async def _write(session: KicadSession, doc: DocKind, *, save_modified: bool = True) -> list[str]:
    await session.save_copy(doc, snapshot_path(session.project, doc), save_modified=save_modified)
    files = await view_files(session, doc)
    if doc == "sch":
        _remove_stale_sheets(session.project, files)
    return files


async def write_kicanvas_snapshot(
    session: KicadSession, doc: DocKind, *, save_modified: bool = True
) -> Path:
    """SaveCopyOfDocument (+ sub-sheet copies) into the view directory; returns the root.
    `save_modified=False`: a schematic with unsaved edits raises `DocumentModified` and
    nothing is written (see `KicadSession.save_copy`)."""
    await _write(session, doc, save_modified=save_modified)
    return snapshot_path(session.project, doc)


async def view_files(session: KicadSession, doc: DocKind) -> list[str]:
    """File names KiCanvas loads for `doc`, relative to the view directory, root first."""
    project = session.project
    if not _project_has(project, doc):
        return []
    root = snapshot_path(project, doc).name
    if doc == "pcb":
        return [root]
    # Normalised like the frames' `view_file_list` names ("./a.kicad_sch" -> "a.kicad_sch"),
    # so a client compares the open response's list with theirs as a set.
    sheets = [PurePosixPath(s).as_posix() for s in await session.call(_sheet_files)]
    others = [s for s in dict.fromkeys(sheets) if s != (project.sch_file or Path()).name]
    return [root, *others]


async def write_view_snapshots(session: KicadSession) -> dict[DocKind, list[str]]:
    """Snapshots of every document the project has (X1); returns the open response's
    `viewFiles` (X3)."""
    out: dict[DocKind, list[str]] = {"sch": [], "pcb": []}
    for doc in ("sch", "pcb"):
        if _project_has(session.project, doc):
            out[doc] = await _write(session, doc)
    return out


def view_bytes(project: Project, files: dict[DocKind, list[str]]) -> dict[str, int]:
    """Size of every listed view file (the open response's `viewBytes`, 6B large-board guard)."""
    out: dict[str, int] = {}
    for name in (n for names in files.values() for n in names):
        path = resolve_view_file(project, name)
        if path is not None:
            out[name] = path.stat().st_size
    return out


def view_file_hashes(project: Project, docs: Iterable[DocKind]) -> dict[str, str]:
    """sha256 of the view files of `docs` by view-relative posix name: every
    `*.kicad_sch` under the view directory for `sch` (sub-sheets incl. stale ones), the
    board snapshot for `pcb`. Symlinks and other non-regular files are skipped; a file
    that cannot be read hashes as "" (unknown: listed as changed), so one bad file never
    fails a publish. Blocking: call it in a thread."""
    root = view_dir(project)
    paths: list[Path] = []
    for doc in set(docs):
        if doc == "sch":
            paths.extend(root.rglob("*.kicad_sch"))
        else:
            paths.append(snapshot_path(project, doc))
    out: dict[str, str] = {}
    for path in paths:
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                continue
        except OSError:  # gone meanwhile (or unreadable directory entry)
            continue
        name = path.relative_to(root).as_posix()
        try:
            digest = _hash_regular(path)
        except OSError:
            digest = ""
        if digest is not None:
            out[name] = digest
    return out


def view_file_list(project: Project, doc: DocKind, names: Iterable[str]) -> list[str]:
    """The view files of `doc` among `names` (view-relative, e.g. the keys of
    `view_file_hashes`), as a view-updated frame's `viewFiles` lists them: the root
    snapshot first - always, even if `names` lacks it - then the others sorted."""
    root = snapshot_path(project, doc).name
    suffix = VIEW_SUFFIXES[doc]
    return [root, *sorted({n for n in names if n.endswith(suffix) and n != root})]


_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)  # a FIFO swapped in never blocks the open
    | getattr(os, "O_BINARY", 0)
)
_CHUNK = 1 << 16


def _hash_regular(path: Path) -> str | None:
    """sha256 of `path` read in chunks from one fd; None if it is no regular file by
    then (swapped for a symlink - the O_NOFOLLOW open fails with ELOOP - or a FIFO after
    the lstat: never followed, never blocks). Other OSErrors propagate."""
    fh = _open_regular(path)
    if fh is None:
        return None
    with fh:
        h = hashlib.sha256()
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
        return h.hexdigest()


def _open_regular(path: Path) -> BinaryIO | None:
    """`path` opened for reading without following a symlink or blocking on a FIFO;
    None if it is a symlink (ELOOP) or no regular file. Other OSErrors propagate."""
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None
        raise
    fh = os.fdopen(fd, "rb")
    if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
        fh.close()
        return None
    return fh


def _inside(base: Path, name: str) -> Path | None:
    """`base / name` if it stays inside the (resolved) `base` without a symlink or `..`
    on the way (the resolved path is the literal one), else None."""
    path = base / name
    if "\x00" in name or path.resolve() != path or path == base:
        return None
    return path


def copy_disk_schematic(project: Project) -> list[str]:
    """Byte copies of the project's schematic files as they are ON DISK (the last save)
    over their view copies, without KiCad: the root sheet, and every sub-sheet the view
    directory already holds whose counterpart exists under the project directory. Names
    that escape the project or view directory, symlinks and non-regular files are
    refused; a file that cannot be copied is logged and skipped. A symlinked
    `.kicad-agent` or view directory, or a view directory resolving outside the project
    directory, is refused as a whole (logged, nothing copied). Returns the view-relative
    names copied. Blocking: call it in a thread.

    Out of the threat model: a same-user race swapping a parent directory for a symlink
    between these checks and the copies - the view directory is owned by the app user,
    who could write anywhere it points anyway."""
    if project.sch_file is None:
        return []
    root_view = view_dir(project)
    if agent_dir(project).is_symlink() or root_view.is_symlink():
        log.warning("view copy from disk refused: symlinked view directory %s", root_view)
        return []
    root_view.mkdir(parents=True, exist_ok=True)
    vroot, proot = root_view.resolve(), project.dir.resolve()
    if not vroot.is_relative_to(proot):
        log.warning("view copy from disk refused: %s is outside the project", vroot)
        return []
    root_name = snapshot_path(project, "sch").name
    names = [root_name]
    for path in sorted(vroot.rglob("*.kicad_sch")):
        name = path.relative_to(vroot).as_posix()
        if name not in (root_name, project.sch_file.name):
            names.append(name)
    copied: list[str] = []
    for name in names:
        dst = _inside(vroot, name)
        src = _inside(proot, project.sch_file.name if name == root_name else name)
        if dst is None or src is None or src.is_relative_to(vroot):
            log.warning("view copy from disk refused for %s", name)
            continue
        try:
            fh = _open_regular(src)
            if fh is None:
                log.warning("view copy from disk refused for %s (no regular file)", name)
                continue
            with fh:
                data = fh.read()
            atomic_write_bytes(dst, data)  # replaces a symlink at dst, never writes through
        except FileNotFoundError:
            continue  # no counterpart on disk (yet): the view copy stays
        except OSError:
            log.warning("view copy from disk failed for %s", name, exc_info=True)
            continue
        copied.append(name)
    return copied


def _remove_stale_sheets(project: Project, current: list[str]) -> None:
    """Sub-sheets that left the hierarchy must not linger in the view directory."""
    root = view_dir(project)
    keep = {(root / name).resolve() for name in current}
    for path in root.rglob("*.kicad_sch"):
        if path.resolve() not in keep:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


def resolve_view_file(project: Project, name: str) -> Path | None:
    """A snapshot file by its view-relative name; None if it is not a safe, existing one."""
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or not name.endswith(tuple(VIEW_SUFFIXES.values()))
    ):
        return None
    root = view_dir(project).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root) or path == root or not path.is_file():
        return None
    return path
