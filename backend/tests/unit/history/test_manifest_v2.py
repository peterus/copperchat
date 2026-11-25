"""Manifest v2, blob refs and blob GC in HistoryStore (FP-C6, index author calls 2 and 17)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from copperchat.db import Database
from copperchat.db.repos.footprints import FootprintWriteRepo
from copperchat.history import store as store_mod
from copperchat.history.blobs import BlobStore
from copperchat.history.restore import snapshot_plan
from copperchat.history.store import (
    GITIGNORE_LINES,
    HistoryStore,
    LibraryLocation,
    LibraryLocator,
    document_files,
)
from copperchat.projects import Project, agent_dir
from tests.support.history import ECC83, copy_project
from tests.unit.history.conftest import Env

# KiCad-saved footprints, copied (never hand-written) into the test library.
_FOOTPRINTS = sorted((ECC83 / "footprints.pretty").glob("*.kicad_mod"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    d = await Database.open(tmp_path / "db.sqlite")
    yield d
    await d.close()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    return r.resolve()


@pytest.fixture
def project(repo: Path) -> Project:
    return copy_project(ECC83, repo / "hardware")


@pytest.fixture
def lib_dir(repo: Path) -> Path:
    d = repo / "footprints" / "X.pretty"
    d.mkdir(parents=True)
    shutil.copyfile(_FOOTPRINTS[0], d / "A.kicad_mod")
    shutil.copyfile(_FOOTPRINTS[1], d / "B.kicad_mod")
    return d


def _locator(repo: Path, lib: Path) -> LibraryLocator:
    async def locate(p: Project) -> LibraryLocation | None:
        return LibraryLocation(dir=lib, repo_root=repo, table=p.dir / "fp-lib-table", nickname="X")

    return locate


async def _none(p: Project) -> LibraryLocation | None:
    return None


def _manifest(store: HistoryStore, sid: str) -> dict[str, object]:
    data = json.loads((store.snapshot_dir(sid) / "manifest.json").read_text())
    assert isinstance(data, dict)
    return data


async def _refs(db: Database, snapshot_id: str | None = None) -> list[tuple[str, str]]:
    if snapshot_id is None:
        rows = await db.fetchall("SELECT snapshot_id, sha256 FROM history_blob_refs")
    else:
        rows = await db.fetchall(
            "SELECT snapshot_id, sha256 FROM history_blob_refs WHERE snapshot_id = ?",
            (snapshot_id,),
        )
    return sorted((r["snapshot_id"], r["sha256"]) for r in rows)


def _blob_names(project: Project) -> list[str]:
    root = agent_dir(project) / "history-blobs"
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


async def test_v2_shape(db: Database, repo: Path, project: Project, lib_dir: Path) -> None:
    journal = FootprintWriteRepo(db)
    await journal.insert_pending(
        project.id, None, "X:A", "footprints/X.pretty/A.kicad_mod", "create", None
    )
    last = await journal.insert_pending(
        project.id, None, "X:B", "footprints/X.pretty/B.kicad_mod", "create", None
    )
    store = HistoryStore(db, project, locate_library=_locator(repo, lib_dir))
    snap = await store.snapshot("run-1", "label")
    m = _manifest(store, snap.id)
    assert m["version"] == 3
    assert m["repoRootRel"] == "hardware/ecc83"
    assert m["journalMaxId"] == last
    files = m["files"]
    assert isinstance(files, list)
    assert {f["relPath"] for f in files} == {
        p.relative_to(project.dir).as_posix() for p in document_files(project)
    }
    assert "fp-lib-table" not in {f["relPath"] for f in files}  # never copied (FP-C6)
    assert sorted(snap.files) == sorted(f["relPath"] for f in files)
    library = m["library"]
    assert isinstance(library, dict)
    assert library["dir"] == "footprints/X.pretty"
    assert library["existed"] is True
    assert library["files"] == [
        {
            "relPath": f"../../footprints/X.pretty/{name}",
            "sha256": _sha(lib_dir / name),
            "size": (lib_dir / name).stat().st_size,
        }
        for name in ("A.kicad_mod", "B.kicad_mod")
    ]
    assert m["table"] == {"existed": True}
    blobs = BlobStore(agent_dir(project))
    for entry in library["files"]:
        assert (
            blobs.path(entry["sha256"]).read_bytes()
            == (lib_dir / Path(entry["relPath"]).name).read_bytes()
        )
    assert await _refs(db, snap.id) == sorted(
        (snap.id, _sha(lib_dir / n)) for n in ("A.kicad_mod", "B.kicad_mod")
    )
    # restore's existing plan still accepts v2 (its files list is unchanged)
    assert len(snapshot_plan(store.snapshot_dir(snap.id), project.dir)) == len(files)


async def test_v2_missing_library_and_table(db: Database, repo: Path, project: Project) -> None:
    (project.dir / "fp-lib-table").unlink()
    lib = repo / "footprints" / "X.pretty"
    store = HistoryStore(db, project, locate_library=_locator(repo, lib))
    snap = await store.snapshot("run-1", "label")
    m = _manifest(store, snap.id)
    assert m["library"] == {"dir": "footprints/X.pretty", "existed": False, "files": []}
    assert m["table"] == {"existed": False}
    assert m["journalMaxId"] == 0
    assert await _refs(db) == []


@pytest.mark.parametrize("locator", ["none", "default"])
async def test_v2_library_null_when_transient(
    db: Database, project: Project, lib_dir: Path, locator: str
) -> None:
    if locator == "none":
        store = HistoryStore(db, project, locate_library=_none)
    else:
        store = HistoryStore(db, project)
    snap = await store.snapshot("run-1", "label")
    m = _manifest(store, snap.id)
    assert m["version"] == 3
    assert m["library"] is None
    assert m["table"] is None
    assert m["repoRootRel"] is None
    assert "journalMaxId" in m
    assert await _refs(db) == []
    assert _blob_names(project) == []


async def test_project_at_repo_root(db: Database, tmp_path: Path) -> None:
    root = (tmp_path / "r").resolve()
    project = copy_project(ECC83, root)
    lib = project.dir / "footprints" / "X.pretty"
    lib.mkdir(parents=True)
    shutil.copyfile(_FOOTPRINTS[0], lib / "A.kicad_mod")
    store = HistoryStore(db, project, locate_library=_locator(project.dir, lib))
    m = _manifest(store, (await store.snapshot("r", "x")).id)
    assert m["repoRootRel"] == "."
    library = m["library"]
    assert isinstance(library, dict)
    assert [f["relPath"] for f in library["files"]] == ["footprints/X.pretty/A.kicad_mod"]


async def test_dedup_across_snapshots(
    db: Database, repo: Path, project: Project, lib_dir: Path
) -> None:
    (lib_dir / "B.kicad_mod").unlink()
    store = HistoryStore(db, project, locate_library=_locator(repo, lib_dir))
    one = await store.snapshot("run-1", "one")
    two = await store.snapshot("run-2", "two")
    sha = _sha(lib_dir / "A.kicad_mod")
    assert _blob_names(project) == [sha]
    assert await _refs(db) == sorted([(one.id, sha), (two.id, sha)])


async def test_blob_dir_fsynced_once_per_snapshot_with_new_blobs(
    db: Database, repo: Path, project: Project, lib_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The renamed blobs' directory entries are made durable (one fsync of the blob dir)
    before their refs are committed, once per snapshot that references blobs."""
    store = HistoryStore(db, project, locate_library=_locator(repo, lib_dir))
    synced: list[tuple[int, int]] = []
    real = os.fsync

    def recording(fd: int) -> None:
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode):
            synced.append((st.st_dev, st.st_ino))
        real(fd)

    monkeypatch.setattr(os, "fsync", recording)
    await store.snapshot("run-1", "one")
    root = (agent_dir(project) / "history-blobs").stat()
    assert synced.count((root.st_dev, root.st_ino)) == 1
    synced.clear()
    await store.snapshot("run-2", "two")  # reused blobs: synced again (an earlier
    # failed snapshot may have renamed them without a sync)
    assert synced.count((root.st_dev, root.st_ino)) == 1


async def test_blob_written_before_manifest(
    db: Database, repo: Path, project: Project, lib_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = HistoryStore(db, project, locate_library=_locator(repo, lib_dir))
    seen: list[list[str]] = []
    real = Path.write_text

    def failing(self: Path, *args: object, **kwargs: object) -> int:
        if self.name == "manifest.json":
            seen.append(_blob_names(project))  # the blobs exist before the manifest
            raise OSError("disk full")
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", failing)
    with pytest.raises(OSError, match="disk full"):
        await store.snapshot("run-1", "label")
    assert seen == [sorted(_sha(lib_dir / n) for n in ("A.kicad_mod", "B.kicad_mod"))]
    assert await store.list() == []
    assert await _refs(db) == []
    assert _blob_names(project) == []  # GC ran in the except path


async def test_failed_snapshot_keeps_referenced_blobs(
    db: Database, repo: Path, project: Project, lib_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = HistoryStore(db, project, locate_library=_locator(repo, lib_dir))
    first = await store.snapshot("run-1", "one")
    kept = sorted(sha for _, sha in await _refs(db, first.id))
    shutil.copyfile(_FOOTPRINTS[2], lib_dir / "C.kicad_mod")
    real = Path.write_text

    def failing(self: Path, *args: object, **kwargs: object) -> int:
        if self.name == "manifest.json":
            raise OSError("disk full")
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", failing)
    with pytest.raises(OSError):
        await store.snapshot("run-2", "two")
    assert _blob_names(project) == kept


async def test_blobs_survive_prune_and_sweep(
    db: Database, repo: Path, project: Project, lib_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_mod, "HISTORY_KEEP", 1)
    (lib_dir / "B.kicad_mod").unlink()
    store = HistoryStore(db, project, locate_library=_locator(repo, lib_dir))
    old_sha = _sha(lib_dir / "A.kicad_mod")
    first = await store.snapshot("run-1", "one")
    assert _blob_names(project) == [old_sha]
    # A stale snapshot dir makes _sweep_orphans run; it must leave the blobs alone.
    (store.root / ".tmp-stale").mkdir()
    shutil.copyfile(_FOOTPRINTS[2], lib_dir / "A.kicad_mod")
    new_sha = _sha(lib_dir / "A.kicad_mod")
    second = await store.snapshot("run-2", "two")
    assert [s.id for s in await store.list()] == [second.id]
    assert not (store.root / ".tmp-stale").exists()
    assert await _refs(db) == [(second.id, new_sha)]
    assert _blob_names(project) == [new_sha]  # the only referencing snapshot was pruned
    assert first.id not in {sid for sid, _ in await _refs(db)}


async def test_prune_gc_removes_tmp_files(
    db: Database, repo: Path, project: Project, lib_dir: Path
) -> None:
    store = HistoryStore(db, project, locate_library=_locator(repo, lib_dir))
    await store.snapshot("run-1", "one")
    tmp = agent_dir(project) / "history-blobs" / ".tmp-crashed"
    tmp.write_bytes(b"partial")
    await store.snapshot("run-2", "two")  # nothing pruned, but a temp file exists
    assert not tmp.exists()
    assert len(_blob_names(project)) == 2


async def test_service_store_passes_locator(env: Env, tmp_path: Path) -> None:
    project = env.project()
    lib = tmp_path / "lib" / "X.pretty"
    lib.mkdir(parents=True)
    shutil.copyfile(_FOOTPRINTS[0], lib / "A.kicad_mod")
    root = tmp_path.resolve()
    env.svc.locate_library = _locator(root, lib)
    store = env.svc.store(project)
    m = _manifest(store, (await store.snapshot("r", "x")).id)
    library = m["library"]
    assert isinstance(library, dict)
    assert library["dir"] == "lib/X.pretty"
    assert [f["sha256"] for f in library["files"]] == [_sha(lib / "A.kicad_mod")]


def test_gitignore_has_blobs() -> None:
    assert "history-blobs/" in GITIGNORE_LINES


async def test_journal_max_id_read_under_library_lock(
    db: Database, repo: Path, project: Project, lib_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """journalMaxId and the library files are read under one library lock: a write of
    another project can never land between the two."""
    from copperchat.history.library_lock import library_lock

    real = FootprintWriteRepo.max_id
    held: list[bool] = []

    async def checked(self: FootprintWriteRepo) -> int:
        held.append(library_lock(lib_dir).locked())
        return await real(self)

    monkeypatch.setattr(FootprintWriteRepo, "max_id", checked)
    await HistoryStore(db, project, locate_library=_locator(repo, lib_dir)).snapshot("r", "l")
    await HistoryStore(db, project, locate_library=_none).snapshot("r", "l")
    assert held == [True, False]
