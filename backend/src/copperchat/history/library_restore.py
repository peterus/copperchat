"""Scoped restore of the repo's local footprint library (spec §10, index FP-C6).

The library is shared by every project of the repo, but history is per project. So a
restore only undoes the library writes **this** project journalled after the snapshot
(``footprint_writes`` rows with ``id > manifest["journalMaxId"]``, author call 17), and
only while nobody else touched the file since. Planning is pure apart from ``lstat``
and directory listings; the caller injects the journal, the cross-project check and a
fresh hash function. ``apply_library_plan`` does the file IO (blocking; the caller runs
it in a thread under the library lock).

Paths: journal ``file_rel`` is repo-root-relative POSIX; manifest ``library.files``
``relPath`` is project-relative (``../../footprints/X.pretty/A.kicad_mod``, or no
``../`` for a project at the repo root). Both are mapped to ``<library dir>/<name>``
and every emitted path is validated (no symlink component from the repo root down,
a ``*.kicad_mod`` directly in the derived library dir, or exactly the project's own
``fp-lib-table``). A forged manifest raises ``CorruptSnapshot``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import posixpath
import re
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from copperchat.db.repos.footprints import ABSENT, FootprintWrite
from copperchat.history.store import CorruptSnapshot, LibraryLocation, sha256_file

_SHA_RE = re.compile(r"[0-9a-f]{64}")
_SUFFIX = ".kicad_mod"
TABLE_NAME = "fp-lib-table"

CHANGED_ELSEWHERE = "changed outside this project, not restored"
NO_VALUE = "no recorded file state, not restored"
NOT_REGULAR = "not a regular file of the local library, not touched"
OUTSIDE_LIBRARY = "not a file of the local library, not restored"


@dataclass
class LibraryRestorePlan:
    copy: list[tuple[str, Path]] = field(default_factory=list)  # (blob sha, target)
    delete: list[Path] = field(default_factory=list)  # *.kicad_mod in the library dir
    rmdir: Path | None = None  # the library dir, non-recursive
    remove_rows: list[str] = field(default_factory=list)  # nicknames, via the API
    remove_table: bool = False  # delete the project's fp-lib-table file
    skipped: list[str] = field(default_factory=list)  # for the restore notice
    refused: str | None = None  # library part refused as a whole (project files still restored)


# --- path validation -------------------------------------------------------------------


def _root(location: LibraryLocation) -> Path:
    return location.repo_root.resolve()


def _root_parts(path: Path, location: LibraryLocation) -> tuple[str, ...]:
    """Components of `path` below the repo root (the unresolved or resolved spelling);
    CorruptSnapshot if it is outside or spelled with `..`."""
    if ".." in path.parts:
        raise CorruptSnapshot(f"path {path} contains '..'")
    target = Path(os.path.normpath(os.path.abspath(path)))
    for base in (Path(os.path.normpath(os.path.abspath(location.repo_root))), _root(location)):
        if target.is_relative_to(base):
            return target.relative_to(base).parts
    raise CorruptSnapshot(f"path {path} is outside the repo root {location.repo_root}")


def _lstat_walk(location: LibraryLocation, parts: tuple[str, ...]) -> Path | None:
    """Walk `parts` from the resolved repo root with lstat. Every existing directory
    component must be a real directory, the last one (if it exists) a regular file.
    Returns the first missing component, or None when everything exists."""
    here = _root(location)
    for i, part in enumerate(parts):
        here = here / part
        try:
            mode = here.lstat().st_mode
        except FileNotFoundError:
            return here
        except OSError as exc:
            raise CorruptSnapshot(f"cannot inspect {here}: {exc}") from exc
        last = i == len(parts) - 1
        if stat.S_ISLNK(mode):
            raise CorruptSnapshot(f"{here} is a symlink")
        if not last and not stat.S_ISDIR(mode):
            raise CorruptSnapshot(f"{here} is not a directory")
        if last and not stat.S_ISREG(mode):
            raise CorruptSnapshot(f"{here} is not a regular file")
    return None


def _library_file_parts(path: Path, location: LibraryLocation) -> tuple[str, ...]:
    parts = _root_parts(path, location)
    lib = _root_parts(location.dir, location)
    if (
        not lib
        or len(parts) != len(lib) + 1
        or parts[:-1] != lib
        or not parts[-1].endswith(_SUFFIX)
        or parts[-1] == _SUFFIX
    ):
        raise CorruptSnapshot(f"{path} is not a footprint directly in the local library")
    return parts


def validate_delete(
    path: Path, location: LibraryLocation, project_dir: Path, *, table: bool
) -> None:
    """A delete target must be an existing regular file without a symlink component
    from the repo root down, and either a `*.kicad_mod` directly in the derived
    library dir or (``table=True``) exactly the project's own fp-lib-table."""
    if table:
        own = Path(os.path.normpath(os.path.abspath(project_dir / TABLE_NAME)))
        given = Path(os.path.normpath(os.path.abspath(path)))
        loc_table = Path(os.path.normpath(os.path.abspath(location.table)))
        if ".." in path.parts or given != own or loc_table != own:
            raise CorruptSnapshot(f"{path} is not this project's {TABLE_NAME}")
        parts = _root_parts(path, location)
    else:
        parts = _library_file_parts(path, location)
    missing = _lstat_walk(location, parts)
    if missing is not None:
        raise CorruptSnapshot(f"{missing} does not exist")


def validate_copy_target(path: Path, location: LibraryLocation) -> None:
    """Same rules as a library delete, but the target may be missing, and the library
    dir itself may be missing if its parent exists as a real directory (it is then
    created non-recursively)."""
    parts = _library_file_parts(path, location)
    missing = _lstat_walk(location, parts)
    if missing is None:
        return
    root = _root(location)
    if missing == root.joinpath(*parts):
        return  # target file missing: fine
    if missing == root.joinpath(*parts[:-1]) and len(parts) >= 2:
        return  # only the library dir is missing; its parent was checked as a real dir
    raise CorruptSnapshot(f"{missing} does not exist; the library dir is never created recursively")


def verify_blob(path: Path, sha: str) -> None:
    """The blob must be a regular file whose content hashes to its name."""
    if not isinstance(sha, str) or _SHA_RE.fullmatch(sha) is None:
        raise CorruptSnapshot(f"invalid blob sha: {sha!r}")
    try:
        regular = stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        regular = False
    if not regular or sha256_file(path) != sha:
        raise CorruptSnapshot(f"library blob {sha} is missing or corrupted")


# --- planning --------------------------------------------------------------------------


def _manifest_rel(value: object, what: str) -> str:
    if not isinstance(value, str) or value.startswith("/"):
        raise CorruptSnapshot(f"snapshot manifest has an invalid {what}: {value!r}")
    return posixpath.normpath(value)  # "" -> "."


def _snapshot_files(library: dict[str, Any], proj_rel: str, lib_rel: str) -> dict[str, str]:
    """name -> blob sha for every manifest library entry (all must be in the lib dir)."""
    entries = library.get("files")
    if not isinstance(entries, list):
        raise CorruptSnapshot("snapshot manifest library.files is not a list")
    files: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise CorruptSnapshot("snapshot manifest library entry is not an object")
        rel = _manifest_rel(entry.get("relPath"), "library path")
        sha = entry.get("sha256")
        if not isinstance(sha, str) or _SHA_RE.fullmatch(sha) is None:
            raise CorruptSnapshot(f"snapshot manifest has an invalid blob sha: {sha!r}")
        joined = posixpath.normpath(posixpath.join("" if proj_rel == "." else proj_rel, rel))
        head, name = posixpath.split(joined)
        if head != lib_rel or not name.endswith(_SUFFIX) or name == _SUFFIX:
            raise CorruptSnapshot(f"snapshot library entry {rel!r} is outside the library")
        files[name] = sha
    return files


def _has_value(row: FootprintWrite) -> bool:
    if row.state == "pending":
        return any(v is not None for v in (row.pre_sha256, row.post_s2_sha256, row.post_sha256))
    return row.post_sha256 is not None


def plan_library_restore(
    manifest: dict[str, Any],
    location: LibraryLocation,
    project_dir: Path,
    journal: list[FootprintWrite],
    foreign_later: Callable[[str, int], bool],
    current: Callable[[Path], str],
    table_rows: list[tuple[str, str]] | None,
) -> LibraryRestorePlan:
    """What a restore of `manifest` does to the shared library (spec §10, binding).

    `journal`: this project's rows after the snapshot (filtered again by id here).
    `foreign_later(file_rel, journalMaxId)`: another project journalled that file
    after the snapshot. `current(path)`: a **fresh** sha256, or ABSENT if missing.
    `table_rows`: the project's current (nickname, uri) rows, None if not open.
    """
    plan = LibraryRestorePlan()
    version = manifest.get("version")
    if version is None or version == 1:
        return plan  # v1 snapshots never touch the library or the table
    if version != 2:
        raise CorruptSnapshot(f"unsupported snapshot manifest version: {version!r}")
    library = manifest.get("library")
    if library is None:
        return plan  # transient repo policy at snapshot time
    if not isinstance(library, dict):
        raise CorruptSnapshot("snapshot manifest library is not an object")
    max_id = manifest.get("journalMaxId")
    if not isinstance(max_id, int) or isinstance(max_id, bool) or max_id < 0:
        raise CorruptSnapshot(f"snapshot manifest has an invalid journalMaxId: {max_id!r}")
    existed = library.get("existed")
    if not isinstance(existed, bool):
        raise CorruptSnapshot("snapshot manifest library.existed is not a boolean")
    table = manifest.get("table")
    table_existed = not (isinstance(table, dict) and table.get("existed") is False)

    try:
        lib_parts = _root_parts(location.dir, location)
        proj_parts = _root_parts(project_dir, location)
    except CorruptSnapshot as exc:
        plan.refused = f"library not restored: {exc}"
        return plan
    lib_rel = "/".join(lib_parts) or "."
    proj_rel = "/".join(proj_parts) or "."
    then_proj = _manifest_rel(manifest.get("repoRootRel"), "repoRootRel")
    then_lib = _manifest_rel(library.get("dir"), "library dir")
    if then_proj != proj_rel:
        plan.refused = (
            f"library not restored: the project was moved or renamed in the repo "
            f"(snapshot: {then_proj}, now: {proj_rel})"
        )
        return plan
    if not lib_parts or then_lib != lib_rel:
        plan.refused = (
            f"library not restored: the snapshot's library dir {then_lib} differs from "
            f"the local library {lib_rel}"
        )
        return plan
    snapshot = _snapshot_files(library, proj_rel, lib_rel)

    rows = sorted((r for r in journal if r.id > max_id), key=lambda r: r.id)
    table_rel = TABLE_NAME if proj_rel == "." else f"{proj_rel}/{TABLE_NAME}"
    nicks: list[str] = []
    latest: dict[str, FootprintWrite] = {}
    for r in rows:
        if r.op == "row_add":  # only feeds remove_rows / remove_table
            if posixpath.normpath(r.file_rel) != table_rel:
                plan.skipped.append(f"library row {r.lib_id}: not this project's table")
            elif r.lib_id not in nicks:
                nicks.append(r.lib_id)
        else:
            latest[r.file_rel] = r  # rows are id-ordered: the last one wins

    lib_dir = _root(location).joinpath(*lib_parts)
    for file_rel, r in sorted(latest.items(), key=lambda kv: kv[1].id):
        label = r.lib_id
        head, name = posixpath.split(posixpath.normpath(file_rel))
        if head != lib_rel or not name.endswith(_SUFFIX) or name == _SUFFIX:
            plan.skipped.append(f"{label}: {OUTSIDE_LIBRARY}")
            continue
        if foreign_later(file_rel, max_id):
            plan.skipped.append(f"{label}: {CHANGED_ELSEWHERE}")
            continue
        if not _has_value(r):
            plan.skipped.append(f"{label}: {NO_VALUE}")
            continue
        target = lib_dir / name
        try:
            _lstat_walk(location, (*lib_parts, name))
        except CorruptSnapshot:
            plan.skipped.append(f"{label}: {NOT_REGULAR}")
            continue
        now = current(target)
        if r.state != "pending" and now != r.post_sha256:
            plan.skipped.append(f"{label}: {CHANGED_ELSEWHERE}")
            continue
        try:
            if name in snapshot:
                if now != snapshot[name]:
                    validate_copy_target(target, location)
                    plan.copy.append((snapshot[name], target))
            elif now != ABSENT:
                validate_delete(target, location, project_dir, table=False)
                plan.delete.append(target)
        except CorruptSnapshot:
            plan.skipped.append(f"{label}: {NOT_REGULAR}")

    if nicks:
        if table_rows is None:
            plan.skipped.extend(f"library row {n} not removed (project not open)" for n in nicks)
        else:
            present = [n for n, _ in table_rows]
            mine = [n for n in nicks if n in present]
            if not table_existed and present and all(n in nicks for n in present):
                try:
                    validate_delete(location.table, location, project_dir, table=True)
                    plan.remove_table = True
                except CorruptSnapshot:
                    plan.remove_rows = mine
            else:
                plan.remove_rows = mine

    # rmdir (judgement call): only a dir that did not exist at snapshot time, when this
    # project added its own library row since (S1 creates row + dir together), that row
    # goes away now (project open), nothing is copied into it, and it would be empty
    # after the planned deletes. Anything else in it (another project's footprint, a
    # skipped file) keeps it.
    if (
        not existed
        and table_rows is not None
        and location.nickname in nicks
        and not plan.copy
        and _real_dir(location, lib_parts)
    ):
        try:
            entries = set(os.listdir(lib_dir))
        except OSError:
            entries = {""}
        if entries <= {p.name for p in plan.delete}:
            plan.rmdir = lib_dir
    return plan


def _real_dir(location: LibraryLocation, parts: tuple[str, ...]) -> bool:
    here = _root(location)
    for part in parts:
        here = here / part
        try:
            if not stat.S_ISDIR(here.lstat().st_mode):
                return False
        except OSError:
            return False
    return True


# --- applying --------------------------------------------------------------------------


def _copy_hashed(src: Path, tmp: Path, sha: str) -> None:
    """Copy the blob to `tmp` while hashing it; CorruptSnapshot on a mismatch."""
    h = hashlib.sha256()
    with src.open("rb") as fin, tmp.open("xb") as fout:
        for chunk in iter(lambda: fin.read(1 << 16), b""):
            h.update(chunk)
            fout.write(chunk)
        fout.flush()
        os.fsync(fout.fileno())
    if h.hexdigest() != sha:
        raise CorruptSnapshot(f"library blob {sha} is corrupted")


def apply_library_plan(
    plan: LibraryRestorePlan,
    blob_path: Callable[[str], Path],
    *,
    location: LibraryLocation,
    project_dir: Path,
) -> list[str]:
    """Blocking: copies first (every blob re-hashed against its name before any target
    is replaced; a bad blob raises CorruptSnapshot and changes nothing), then deletes,
    the table file and the rmdir. Every path is re-validated. Returns notes for the
    restore notice (deletes that became unsafe or failed, a dir that is not empty)."""
    notes: list[str] = []
    staged: list[tuple[Path, Path]] = []
    created_dirs: list[Path] = []
    try:
        for sha, target in plan.copy:
            validate_copy_target(target, location)
            src = blob_path(sha)
            verify_blob(src, sha)
            if not target.parent.exists():
                target.parent.mkdir()  # non-recursive, below a real footprints/ dir
                created_dirs.append(target.parent)
            tmp = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}")
            staged.append((tmp, target))
            _copy_hashed(src, tmp, sha)
    except BaseException:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
        for d in reversed(created_dirs):
            with contextlib.suppress(OSError):
                d.rmdir()
        raise
    for tmp, target in staged:
        os.replace(tmp, target)

    for path in plan.delete:
        try:
            validate_delete(path, location, project_dir, table=False)
            path.unlink()
        except (CorruptSnapshot, OSError) as exc:
            notes.append(f"{path.name} not deleted: {exc}")
    if plan.remove_table:
        try:
            validate_delete(location.table, location, project_dir, table=True)
            location.table.unlink()
        except (CorruptSnapshot, OSError) as exc:
            notes.append(f"{TABLE_NAME} not removed: {exc}")
    if plan.rmdir is not None:
        try:
            parts = _root_parts(plan.rmdir, location)
            if parts != _root_parts(location.dir, location) or not _real_dir(location, parts):
                raise CorruptSnapshot(f"{plan.rmdir} is not the local library dir")
            os.rmdir(plan.rmdir)
        except (CorruptSnapshot, OSError) as exc:
            notes.append(f"library directory not removed: {exc}")
    return notes
