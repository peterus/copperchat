"""The sibling-project scan of `fp_delete` (spec §7.8, F4b Task 2): a read-only byte
search for `"<lib_id>"` in the `*.kicad_sch` / `*.kicad_pcb` files of the repo's other
projects. CopperChat cannot ask their KiCad sessions (C14), so a hit is only a warning
(`possibly_in_use_elsewhere`). Nothing is parsed or written.

Blocking: run it with `asyncio.to_thread`. The walk prunes hidden dirs, `SKIP_DIRS` and
symlinks like `document_files`, but **descends** into dirs with their own `*.kicad_pro`
(those are the siblings it must search); it skips only the own project's document files,
not its subtree (a project at the repo root would otherwise hide every nested sibling).
Only regular files are opened (`lstat` + `S_ISREG`, then `O_NOFOLLOW|O_NONBLOCK` and an
`fstat` re-check), and every cap marks the result incomplete."""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from copperchat.history.store import document_files
from copperchat.projects import SKIP_DIRS, Project

log = logging.getLogger(__name__)

SUFFIXES = (".kicad_sch", ".kicad_pcb")
CHUNK_BYTES = 1024 * 1024  # streamed in 1 MB chunks
MAX_FILES = 500  # files opened
MAX_FILE_BYTES = 20 * 1024 * 1024  # a larger file is skipped
MAX_TOTAL_BYTES = 200 * 1024 * 1024  # bytes read over all files
MAX_DIRS = 20_000  # directories visited

_OPEN_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)) | getattr(
    os, "O_NOCTTY", 0
)


@dataclass(frozen=True)
class ScanResult:
    """Repo-relative POSIX paths of the files naming the lib id (sorted), and whether
    every candidate file was searched (False once any cap was hit)."""

    hits: list[str] = field(default_factory=list)
    complete: bool = True


def _own_documents(own_project_dir: Path) -> set[Path]:
    """`document_files` of every project file in the own project's directory."""
    out: set[Path] = set()
    try:
        names = sorted(os.listdir(own_project_dir))
    except OSError:
        return out
    for name in names:
        pro = own_project_dir / name
        if not name.endswith(".kicad_pro") or name.startswith(".") or pro.is_symlink():
            continue
        pcb = pro.with_suffix(".kicad_pcb")
        project = Project(
            id="",
            name=pro.stem,
            rel_path="",
            dir=own_project_dir,
            pro_file=pro,
            sch_file=None,
            pcb_file=pcb if pcb.is_file() and not pcb.is_symlink() else None,
        )
        out.update(document_files(project))
    return out


class _Budget:
    def __init__(self) -> None:
        self.files = 0
        self.bytes = 0
        self.complete = True


def _contains(path: Path, needle: bytes, budget: _Budget) -> bool:
    """Stream the regular file `path` looking for `needle`; the
    file and the total byte caps hold for what is actually read."""
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except OSError as exc:  # swapped for a symlink (ELOOP), vanished, unreadable
        log.info("usage scan: %s not opened: %s", path, exc)
        return False
    with os.fdopen(fd, "rb", buffering=0) as fh:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return False
        if st.st_size > MAX_FILE_BYTES or budget.bytes + st.st_size > MAX_TOTAL_BYTES:
            budget.complete = False
            return False
        limit = min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - budget.bytes)
        overlap = len(needle)
        tail = b""
        read = 0
        while True:
            chunk = fh.read(CHUNK_BYTES)
            if not chunk:
                return False
            read += len(chunk)
            budget.bytes += len(chunk)
            if read > limit:  # the file grew past a cap while being read
                budget.complete = False
                return False
            data = tail + chunk
            if needle in data:
                return True
            tail = data[-overlap:]


def scan_other_projects(repo_root: Path, own_project_dir: Path, lib_id: str) -> ScanResult:
    """Search the repo's `*.kicad_sch` / `*.kicad_pcb` files (except the own project's
    documents) for `"<lib_id>"`, the exact quoted string a symbol's Footprint field or a
    board footprint carries. Caps: `MAX_FILES` files, `MAX_FILE_BYTES` per file,
    `MAX_TOTAL_BYTES` in total, `MAX_DIRS` directories; any of them -> `complete=False`."""
    root = repo_root.resolve()
    own = _own_documents(own_project_dir.resolve())
    needle = b'"' + lib_id.encode("utf-8") + b'"'
    budget = _Budget()
    hits: list[str] = []
    dirs_seen = 0
    for here, dirs, names in os.walk(root, followlinks=False):
        dirs_seen += 1
        if dirs_seen > MAX_DIRS:
            budget.complete = False
            break
        base = Path(here)
        dirs[:] = sorted(
            d
            for d in dirs
            if not d.startswith(".") and d not in SKIP_DIRS and not (base / d).is_symlink()
        )
        for name in sorted(names):
            if not name.endswith(SUFFIXES):
                continue
            path = base / name
            if path in own:
                continue
            try:
                st = path.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):  # symlink, FIFO, device: never opened
                continue
            if st.st_size > MAX_FILE_BYTES or budget.bytes + st.st_size > MAX_TOTAL_BYTES:
                budget.complete = False
                continue
            if budget.files >= MAX_FILES:
                budget.complete = False
                return ScanResult(sorted(hits), False)
            budget.files += 1
            if _contains(path, needle, budget):
                hits.append(path.relative_to(root).as_posix())
    return ScanResult(sorted(hits), budget.complete)
