"""Per-turn snapshots of KiCad's own saved files.

Snapshots are byte-for-byte copies of files KiCad wrote (SaveDocument). CopperChat
never parses or edits their content; it only copies bytes and hashes them.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from copperchat.db import Database, utcnow_iso
from copperchat.db.repos.footprints import ABSENT, FootprintWriteRepo
from copperchat.errors import CopperChatError
from copperchat.history.blobs import BlobStore, blob_lock, sha256_regular
from copperchat.history.blobs import CorruptSnapshot as CorruptSnapshot
from copperchat.history.blobs import sha256_file as sha256_file
from copperchat.history.library_lock import library_lock
from copperchat.plans.repo import PlanRepo
from copperchat.projects import SKIP_DIRS, Project, agent_dir

HISTORY_KEEP = 50  # turn snapshots; step checkpoints are pruned with their turn (§5.4)
# Newest plan-step checkpoints kept per project (E8b: a long plan, revisions and
# rollbacks within a few turns would otherwise grow them without bound). Snapshots an
# open plan can still roll back to are never pruned.
STEP_KEEP = 100
SNAPSHOT_KINDS = ("turn", "step")
# CopperChat's caches inside `.kicad-agent/`; kept out of the user's git repository.
GITIGNORE_LINES = ("history/", "history-blobs/", "view/", "renders/", "uploads/", "images/")
MANIFEST_VERSION = 3
# The board-side files a snapshot records as `absent` when they are not there (v3,
# spec §6.4): restoring the snapshot deletes them again (undo of a created board).
ABSENT_SUFFIXES = (".kicad_pcb", ".kicad_dru")
LABEL_MAX = 200
_ID_RE = re.compile(r"[0-9a-f]{32}")


class InvalidSnapshotId(CopperChatError):
    code = "invalid_snapshot_id"
    http_status = 400


class SnapshotNotFound(CopperChatError):
    code = "snapshot_not_found"
    http_status = 404


@dataclass(frozen=True)
class Snapshot:
    id: str
    project_id: str
    run_id: str
    created_at: str
    label: str
    files: list[str]
    has_preview: bool
    kind: str = "turn"  # turn | step (plan-step checkpoint, E-C10)
    plan_step_id: str | None = None


def validate_snapshot_id(raw: str) -> str:
    if not isinstance(raw, str) or _ID_RE.fullmatch(raw) is None:
        raise InvalidSnapshotId(f"invalid snapshot id: {raw!r}")
    return raw


def _is_document_name(name: str) -> bool:
    # KiCad GUI autosaves (`_autosave-x.kicad_sch`) and lock/backup names are not documents.
    return name.endswith(".kicad_sch") and not name.startswith(("_autosave-", "~", "."))


def document_files(project: Project) -> list[Path]:
    """The project file, every schematic sheet below the project dir, the board, and
    the board's custom rules file `<stem>.kicad_dru` (KiCad writes it on
    SetCustomDesignRules) when it is a regular file (lstat: never a symlink).

    Pruned from the walk, never descended into: hidden directories (`.git`,
    `.kicad-agent` with its snapshots and view copies), the discovery skip list, and
    sub-directories that hold their own `.kicad_pro` (a separate project).
    """
    files: list[Path] = [project.pro_file]
    sheets: list[Path] = []
    for here, dirs, names in os.walk(project.dir):
        root = Path(here)
        dirs[:] = [
            d
            for d in dirs
            if not d.startswith(".")
            and d not in SKIP_DIRS
            and not (root / d).is_symlink()
            and not any((root / d).glob("*.kicad_pro"))
        ]
        for name in names:
            path = root / name
            if _is_document_name(name) and path.is_file() and not path.is_symlink():
                sheets.append(path)
    files.extend(sorted(sheets))
    # lstat (never followed): a board swapped for a symlink is neither copied nor
    # deleted - it is recorded `absent` and a restore skips it with a warning.
    if project.pcb_file is not None and _regular_file(project.pcb_file):
        files.append(project.pcb_file)
    dru = project.pro_file.with_suffix(".kicad_dru")
    if _regular_file(dru):
        files.append(dru)
    return files


def absent_files(project: Project) -> list[str]:
    """Project-relative POSIX names of `<stem>.kicad_pcb` / `<stem>.kicad_dru` that do
    not exist, or are symlinks, right now (the manifest's `absent`, v3)."""
    out: list[str] = []
    for suffix in ABSENT_SUFFIXES:
        path = project.pro_file.with_suffix(suffix)
        try:
            st = path.lstat()
        except FileNotFoundError:
            out.append(path.relative_to(project.dir).as_posix())
            continue
        if stat.S_ISLNK(st.st_mode):
            out.append(path.relative_to(project.dir).as_posix())
    return out


@dataclass(frozen=True)
class LibraryLocation:
    """The local footprint library as history sees it (FP-C6; F4a adapts LocalLibrary)."""

    dir: Path
    repo_root: Path
    table: Path  # the project's fp-lib-table (may not exist)
    nickname: str


@dataclass(frozen=True)
class TrackedFile:
    path: Path
    rel: str  # project-relative POSIX path; library files may start with "../"
    library: bool


# Finds the project's local footprint library (None: the project has none).
LibraryLocator = Callable[[Project], Awaitable[LibraryLocation | None]]


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _library_dir(location: LibraryLocation) -> Path | None:
    """The library dir below the resolved repo root, or None if it is missing or any
    component from the repo root down is a symlink. Outside the repo root: ValueError."""
    root = location.repo_root.resolve()
    target = Path(os.path.normpath(os.path.abspath(location.dir)))
    for base in (Path(os.path.normpath(os.path.abspath(location.repo_root))), root):
        if target.is_relative_to(base):
            parts = target.relative_to(base).parts
            break
    else:
        raise ValueError(f"library dir {location.dir} is outside the repo root {root}")
    here = root
    for part in parts:
        here = here / part
        try:
            if not stat.S_ISDIR(here.lstat().st_mode):
                return None
        except OSError:
            return None
    return here


def tracked_files(project: Project, library: LibraryLocation | None) -> list[TrackedFile]:
    """document_files plus, with a library, the project's fp-lib-table and every
    `*.kicad_mod` directly in the local library dir (regular files only, no symlinks)."""
    files = [
        TrackedFile(p, p.relative_to(project.dir).as_posix(), False)
        for p in document_files(project)
    ]
    if library is None:
        return files
    lib_dir = _library_dir(library)  # validates containment even when the table is absent
    if _regular_file(library.table):
        rel = Path(os.path.relpath(library.table, project.dir)).as_posix()
        files.append(TrackedFile(library.table, rel, False))
    if lib_dir is None:
        return files
    for path in sorted(lib_dir.glob("*.kicad_mod")):
        if _regular_file(path):
            rel = Path(os.path.relpath(path, project.dir)).as_posix()
            files.append(TrackedFile(path, rel, True))
    return files


# Presence rows (FP-C6): hash rows that stand for no file content. With a library
# located, the current hashes carry
# - the library MARKER: rel = the library dir's project-relative POSIX path + "/",
#   sha LIBRARY_PRESENT ("dir") or ABSENT if the dir is missing, and
# - the project's fp-lib-table with sha ABSENT when it does not exist (a present table
#   is a tracked file with its real sha).
# A stored marker means "the library was tracked when these rows were recorded": a
# footprint (or table) without a stored row is then a change, not a baseline
# (`hooks.changed_rels`). Marker rows are never hashed, reported or passed to listeners.
LIBRARY_PRESENT = "dir"


def is_marker_rel(rel: str) -> bool:
    """The library marker row (no file: its rel ends with "/")."""
    return rel.endswith("/")


def library_marker_rel(project: Project, library: LibraryLocation) -> str:
    return Path(os.path.relpath(library.dir, project.dir)).as_posix() + "/"


def with_presence_rows(
    rows: Mapping[str, HashRow], project: Project, library: LibraryLocation | None
) -> dict[str, HashRow]:
    """`rows` (hashes of `tracked_files`) plus the presence rows (blocking: one lstat
    per library dir component). No library: `rows` unchanged."""
    out = dict(rows)
    if library is None:
        return out
    present = _library_dir(library) is not None
    out[library_marker_rel(project, library)] = HashRow(LIBRARY_PRESENT if present else ABSENT)
    table = Path(os.path.relpath(library.table, project.dir)).as_posix()
    out.setdefault(table, HashRow(ABSENT))
    return out


def repo_rel(path: Path, repo_root: Path) -> str:
    """`path` relative to the repo root as POSIX ("." for the root itself); the root
    may be given resolved or not. Outside the repo root: ValueError."""
    target = Path(os.path.normpath(os.path.abspath(path)))
    for base in (Path(os.path.normpath(os.path.abspath(repo_root))), repo_root.resolve()):
        if target.is_relative_to(base):
            return target.relative_to(base).as_posix()
    raise ValueError(f"{path} is outside the repo root {repo_root}")


def compute_hashes(project: Project) -> dict[str, str]:
    """rel posix path -> sha256 of every document file (blocking; see compute_hashes_async)."""
    return {p.relative_to(project.dir).as_posix(): sha256_file(p) for p in document_files(project)}


async def compute_hashes_async(project: Project) -> dict[str, str]:
    return await asyncio.to_thread(compute_hashes, project)


# A file whose mtime or ctime is this close to now may still be written to within the
# same timestamp granularity ("racily clean"): its stats are never stored for reuse.
RACY_NS = 2_000_000_000
_now_ns = time.time_ns  # the racily-clean clock (tests shift it)


@dataclass(frozen=True)
class HashRow:
    """A stored document hash; the stats are None when the row must never be reused."""

    sha256: str
    size: int | None = None
    mtime_ns: int | None = None
    inode: int | None = None
    ctime_ns: int | None = None


def _stat_key(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_size, st.st_mtime_ns, st.st_ino, st.st_ctime_ns)


def _reusable(row: HashRow | None, st: os.stat_result) -> bool:
    if row is None:
        return False
    key = (row.size, row.mtime_ns, row.inode, row.ctime_ns)
    return None not in key and key == _stat_key(st)


def compute_tracked_hashes(
    files: list[TrackedFile], previous: dict[str, HashRow]
) -> dict[str, HashRow]:
    """rel -> HashRow for every file still present (blocking; FP-C7). A library file
    whose lstat (size, mtime_ns, inode, ctime_ns) equals its previous row's reuses that sha
    without reading the file; project files are always hashed. A row gets stats only
    if they did not change while hashing and neither the mtime nor the ctime is within
    RACY_NS of now. Presence rows are added by `with_presence_rows`, never here."""
    rows: dict[str, HashRow] = {}
    for f in files:
        # Library and table files are shared with other projects / the KiCad GUI:
        # never follow a symlink or block on a FIFO swapped in after the listing.
        outside = f.library or f.path.name == "fp-lib-table"
        try:
            before = f.path.lstat()
            prev = previous.get(f.rel)
            if f.library and _reusable(prev, before):
                assert prev is not None
                rows[f.rel] = prev
                continue
            digest = sha256_regular(f.path) if outside else sha256_file(f.path)
            after = f.path.lstat()
        except FileNotFoundError:  # vanished between listing and hashing
            continue
        except OSError:
            if not outside:
                raise
            continue  # no longer a regular file: not tracked, like in tracked_files
        stable = _stat_key(before) == _stat_key(after)
        now = _now_ns()
        clean = all(abs(now - t) >= RACY_NS for t in (after.st_mtime_ns, after.st_ctime_ns))
        if stable and clean:
            rows[f.rel] = HashRow(digest, *_stat_key(after))
        else:
            rows[f.rel] = HashRow(digest)
    return rows


def ensure_gitignore(project: Project) -> None:
    """CopperChat's own directory; keeps its caches out of the user's git repo."""
    path = agent_dir(project) / ".gitignore"
    content = "\n".join(GITIGNORE_LINES) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text() != content:
        path.write_text(content)


class HistoryStore:
    def __init__(
        self, db: Database, project: Project, *, locate_library: LibraryLocator | None = None
    ) -> None:
        self._db = db
        self.project = project
        self.root = agent_dir(project) / "history"
        self.blobs = BlobStore(agent_dir(project))
        self._locate_library = locate_library

    def snapshot_dir(self, snapshot_id: str) -> Path:
        sid = validate_snapshot_id(snapshot_id)
        path = (self.root / sid).resolve()
        if path.parent != self.root.resolve():
            raise InvalidSnapshotId(f"invalid snapshot id: {snapshot_id!r}")
        return path

    def _copy_documents(self, tmp: Path) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for src in document_files(self.project):
            rel = src.relative_to(self.project.dir)
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            entries.append(
                {
                    "relPath": rel.as_posix(),
                    "sha256": sha256_file(target),
                    "size": target.stat().st_size,
                }
            )
        return entries

    def _store_library(
        self, location: LibraryLocation, previous: Mapping[str, HashRow]
    ) -> dict[str, Any]:
        """The manifest's `library` part; puts every library file into the blob store
        (blocking; the caller holds the library and blob locks). A stored hash whose
        (size, mtime_ns, inode, ctime_ns) still matches names the blob without reading the file."""
        exists = _library_dir(location) is not None
        entries: list[dict[str, Any]] = []
        for f in tracked_files(self.project, location):
            if not f.library:
                continue
            try:
                prev = previous.get(f.rel)
                known = (
                    prev.sha256 if prev is not None and _reusable(prev, f.path.lstat()) else None
                )
                sha, size = self.blobs.put(f.path, known)
            except FileNotFoundError:  # vanished between listing and copying
                continue
            entries.append({"relPath": f.rel, "sha256": sha, "size": size})
        if entries:  # durable dir entries before the refs are committed
            self.blobs.sync_dir()
        return {
            "dir": repo_rel(location.dir, location.repo_root),
            "existed": exists,
            "files": entries,
        }

    async def _blob_refs(self) -> set[str]:
        rows = await self._db.fetchall(
            "SELECT DISTINCT sha256 FROM history_blob_refs WHERE project_id = ?",
            (self.project.id,),
        )
        return {str(r["sha256"]) for r in rows}

    async def _gc_blobs_locked(self) -> int:
        """Blobs no snapshot row references, and every temp file (blob lock held)."""
        return await asyncio.to_thread(self.blobs.gc, await self._blob_refs())

    def _sweep_orphans(self, known: set[str]) -> None:
        """Snapshot dirs without a DB row (killed mid-snapshot) and stale temp dirs."""
        if not self.root.is_dir():
            return
        for child in self.root.iterdir():
            if child.is_dir() and not child.is_symlink() and child.name not in known:
                shutil.rmtree(child, ignore_errors=True)

    async def snapshot(
        self,
        run_id: str,
        label: str,
        preview_png: bytes | None = None,
        *,
        kind: str = "turn",
        plan_step_id: str | None = None,
    ) -> Snapshot:
        if kind not in SNAPSHOT_KINDS:
            raise ValueError(f"unknown snapshot kind {kind!r}")
        ensure_gitignore(self.project)
        location = (
            None if self._locate_library is None else await self._locate_library(self.project)
        )
        sid = uuid.uuid4().hex
        dest = self.snapshot_dir(sid)
        tmp = self.root / f".tmp-{sid}"
        label = label[:LABEL_MAX]
        journal = FootprintWriteRepo(self._db)
        journal_max_id: int | None = None
        library: dict[str, Any] | None = None
        blobs_lock = blob_lock(self.project.id)
        blobs_held = False
        renamed = False
        try:
            if location is not None:
                previous = await DocumentHashRepo(self._db).get_rows(self.project.id)
                # The library part only (index lock order: library lock -> blob lock).
                # The blob lock stays held until the refs are committed, so a GC
                # never removes a blob that is about to be referenced.
                async with library_lock(location.dir):
                    # Under the library lock: no other project's write lands between
                    # the journal mark and the library files stored below.
                    journal_max_id = await journal.max_id()
                    await blobs_lock.acquire()
                    blobs_held = True
                    library = await asyncio.to_thread(self._store_library, location, previous)
            else:
                journal_max_id = await journal.max_id()
            entries = await asyncio.to_thread(self._copy_documents, tmp)
            # A separate pass: a file created between the two is copied AND would be
            # listed absent (a restore would then delete it). A copied file wins.
            copied = {e["relPath"] for e in entries}
            absent = [
                rel
                for rel in await asyncio.to_thread(absent_files, self.project)
                if rel not in copied
            ]
            created = utcnow_iso()
            if preview_png is not None:
                (tmp / "preview.png").write_bytes(preview_png)
            manifest = {
                "version": MANIFEST_VERSION,
                "id": sid,
                "projectId": self.project.id,
                "runId": run_id,
                "createdAt": created,
                "label": label,
                "kind": kind,
                "planStepId": plan_step_id,
                "repoRootRel": (
                    None if location is None else repo_rel(self.project.dir, location.repo_root)
                ),
                "journalMaxId": journal_max_id,
                "files": entries,
                "absent": absent,
                "library": library,
                "table": None if location is None else {"existed": _regular_file(location.table)},
            }
            (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2))
            tmp.rename(dest)
            renamed = True
            files = [e["relPath"] for e in entries]
            shas = sorted({e["sha256"] for e in (library or {}).get("files", [])})
            async with self._db.transaction():
                await self._db.execute(
                    "INSERT INTO history_snapshots(id, project_id, run_id, created_at, label,"
                    " files, kind, plan_step_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        sid,
                        self.project.id,
                        run_id,
                        created,
                        label,
                        json.dumps(files),
                        kind,
                        plan_step_id,
                    ),
                )
                await self._db.executemany(
                    "INSERT INTO history_blob_refs(project_id, snapshot_id, sha256)"
                    " VALUES (?, ?, ?)",
                    [(self.project.id, sid, sha) for sha in shas],
                )
        except BaseException:
            await asyncio.to_thread(shutil.rmtree, dest if renamed else tmp, True)
            if blobs_held:  # blobs written for this snapshot are orphans now
                with suppress(Exception):
                    await self._gc_blobs_locked()
            raise
        finally:
            if blobs_held:
                blobs_lock.release()
        await self._prune()
        return Snapshot(
            sid,
            self.project.id,
            run_id,
            created,
            label,
            files,
            preview_png is not None,
            kind,
            plan_step_id,
        )

    def _row(self, row: Any) -> Snapshot:
        sid = str(row["id"])
        return Snapshot(
            sid,
            row["project_id"],
            row["run_id"],
            row["created_at"],
            row["label"],
            json.loads(row["files"]),
            (self.root / sid / "preview.png").is_file(),
            str(row["kind"]),
            row["plan_step_id"],
        )

    async def list(self) -> list[Snapshot]:
        """Newest first."""
        rows = await self._db.fetchall(
            "SELECT * FROM history_snapshots WHERE project_id = ?"
            " ORDER BY created_at DESC, rowid DESC",
            (self.project.id,),
        )
        return [self._row(r) for r in rows]

    async def get(self, snapshot_id: str) -> Snapshot:
        sid = validate_snapshot_id(snapshot_id)
        row = await self._db.fetchone(
            "SELECT * FROM history_snapshots WHERE id = ? AND project_id = ?",
            (sid, self.project.id),
        )
        if row is None or not (self.snapshot_dir(sid) / "manifest.json").is_file():
            raise SnapshotNotFound(f"snapshot {sid} not found")
        return self._row(row)

    async def _latest(self, run_id: str, kind: str) -> Snapshot | None:
        row = await self._db.fetchone(
            "SELECT * FROM history_snapshots WHERE project_id = ? AND run_id = ? AND kind = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (self.project.id, run_id, kind),
        )
        return None if row is None else self._row(row)

    async def latest_for_run(self, run_id: str) -> Snapshot | None:
        """The run's turn snapshot (its undo point); step checkpoints never count."""
        return await self._latest(run_id, "turn")

    async def latest_for_runs(self, run_ids: Iterable[str]) -> dict[str, Snapshot]:
        """`latest_for_run` for many runs in one query; runs without one are absent."""
        runs = sorted(set(run_ids))
        if not runs:
            return {}
        marks = ", ".join("?" for _ in runs)
        rows = await self._db.fetchall(
            "SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY run_id"
            " ORDER BY created_at DESC, rowid DESC) AS rn FROM history_snapshots"
            f" WHERE project_id = ? AND kind = 'turn' AND run_id IN ({marks})) WHERE rn = 1",
            (self.project.id, *runs),
        )
        return {str(r["run_id"]): self._row(r) for r in rows}

    async def latest_step_checkpoint(self, run_id: str) -> Snapshot | None:
        return await self._latest(run_id, "step")

    async def _prune(self) -> None:
        """HISTORY_KEEP turn snapshots; step checkpoints go with their turn (same run),
        and so do those of other runs older than the oldest kept turn snapshot (a run
        whose steps changed nothing has no turn snapshot of its own); at most STEP_KEEP
        step checkpoints. An open plan's base snapshot and step checkpoints are kept."""
        turns = await self._db.fetchall(
            "SELECT id, run_id, created_at, rowid AS rid FROM history_snapshots"
            " WHERE project_id = ? AND kind = 'turn'"
            " ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?",
            (self.project.id, HISTORY_KEEP - 1),
        )
        if len(turns) > 1:
            oldest_kept, pruned = turns[0], turns[1:]
            runs = sorted({str(r["run_id"]) for r in pruned})
            marks = ", ".join("?" for _ in runs)
            rows = list(pruned) + list(
                await self._db.fetchall(
                    "SELECT id FROM history_snapshots WHERE project_id = ? AND kind = 'step'"
                    f" AND (run_id IN ({marks}) OR (run_id != ? AND (created_at < ?"
                    " OR (created_at = ? AND rowid < ?))))",
                    (
                        self.project.id,
                        *runs,
                        oldest_kept["run_id"],
                        oldest_kept["created_at"],
                        oldest_kept["created_at"],
                        oldest_kept["rid"],
                    ),
                )
            )
        else:
            rows = []
        rows += await self._db.fetchall(
            "SELECT id FROM history_snapshots WHERE project_id = ? AND kind = 'step'"
            " ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?",
            (self.project.id, STEP_KEEP),
        )
        protected = await PlanRepo(self._db).protected_snapshot_ids(self.project.id)
        doomed = list(dict.fromkeys(str(r["id"]) for r in rows if str(r["id"]) not in protected))
        for sid in doomed:
            async with self._db.transaction():
                await self._db.execute(
                    "DELETE FROM history_blob_refs WHERE snapshot_id = ?", (sid,)
                )
                await self._db.execute("DELETE FROM history_snapshots WHERE id = ?", (sid,))
            await asyncio.to_thread(shutil.rmtree, self.snapshot_dir(sid), True)
        kept = await self._db.fetchall(
            "SELECT id FROM history_snapshots WHERE project_id = ?", (self.project.id,)
        )
        await asyncio.to_thread(self._sweep_orphans, {str(r["id"]) for r in kept})
        # Rows and dirs first, blobs last: a crash in between leaves unreferenced
        # blobs, never dangling references (spec §10 "Pruning").
        if bool(doomed) or await asyncio.to_thread(self.blobs.has_temp_files):
            async with blob_lock(self.project.id):
                await self._gc_blobs_locked()


class DocumentHashRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, project_id: str) -> dict[str, str]:
        rows = await self._db.fetchall(
            "SELECT rel_path, sha256 FROM document_hashes WHERE project_id = ?", (project_id,)
        )
        return {r["rel_path"]: r["sha256"] for r in rows}

    async def get_rows(self, project_id: str) -> dict[str, HashRow]:
        rows = await self._db.fetchall(
            "SELECT rel_path, sha256, size, mtime_ns, inode, ctime_ns FROM document_hashes"
            " WHERE project_id = ?",
            (project_id,),
        )
        return {
            r["rel_path"]: HashRow(r["sha256"], r["size"], r["mtime_ns"], r["inode"], r["ctime_ns"])
            for r in rows
        }

    async def _insert(self, project_id: str, hashes: Mapping[str, HashRow | str]) -> None:
        def params(rel: str, value: HashRow | str) -> tuple[Any, ...]:
            row = HashRow(value) if isinstance(value, str) else value
            return (project_id, rel, row.sha256, row.size, row.mtime_ns, row.inode, row.ctime_ns)

        await self._db.executemany(
            "INSERT OR REPLACE INTO document_hashes(project_id, rel_path, sha256, size,"
            " mtime_ns, inode, ctime_ns) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [params(rel, value) for rel, value in sorted(hashes.items())],
        )

    async def replace(self, project_id: str, hashes: Mapping[str, HashRow | str]) -> None:
        """Replace every stored row; a plain digest is stored without stats."""
        async with self._db.transaction():
            await self._db.execute(
                "DELETE FROM document_hashes WHERE project_id = ?", (project_id,)
            )
            await self._insert(project_id, hashes)

    async def upsert(
        self,
        project_id: str,
        rows: Mapping[str, HashRow | str],
        delete: Iterable[str] = (),
    ) -> None:
        """Write `rows` and remove the rows of `delete`, in one transaction; every other
        row of the project stays as it is (no read-modify-write of the whole set)."""
        gone = sorted(set(delete) - rows.keys())
        async with self._db.transaction():
            await self._db.executemany(
                "DELETE FROM document_hashes WHERE project_id = ? AND rel_path = ?",
                [(project_id, rel) for rel in gone],
            )
            await self._insert(project_id, rows)
