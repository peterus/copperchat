"""Stat-keyed hash reuse for library files (FP-C7, spec §10 "Hashing")."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from copperchat.history import store as store_mod
from copperchat.history.store import (
    DocumentHashRepo,
    HashRow,
    LibraryLocation,
    TrackedFile,
    compute_tracked_hashes,
    sha256_file,
    tracked_files,
)
from copperchat.projects import Project
from tests.support.history import ECC83, copy_project
from tests.unit.history.conftest import Env

# KiCad-saved footprints, copied (never hand-written) into the test library.
_FOOTPRINTS = sorted((ECC83 / "footprints.pretty").glob("*.kicad_mod"))
_OLD = 10  # seconds in the past: well outside the racily-clean window


def _age(path: Path, seconds: float = _OLD) -> None:
    t = time.time_ns() - int(seconds * 1e9)
    os.utime(path, ns=(t, t))


@pytest.fixture
def lib(tmp_path: Path) -> Path:
    d = tmp_path / "lib" / "X.pretty"
    d.mkdir(parents=True)
    for name, src in (("A.kicad_mod", _FOOTPRINTS[0]), ("B.kicad_mod", _FOOTPRINTS[1])):
        shutil.copyfile(src, d / name)
        _age(d / name)
    return d


def _files(lib: Path) -> list[TrackedFile]:
    return [TrackedFile(p, f"../lib/X.pretty/{p.name}", True) for p in sorted(lib.iterdir())]


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []

    def counting(path: Path) -> str:
        calls.append(path)
        return sha256_file(path)

    monkeypatch.setattr(store_mod, "sha256_file", counting)
    return calls


def test_unchanged_stat_reuses(lib: Path, counted: list[Path]) -> None:
    files = _files(lib)
    first = compute_tracked_hashes(files, {})
    assert len(counted) == 2
    assert all(r.size is not None and r.mtime_ns is not None for r in first.values())
    counted.clear()
    second = compute_tracked_hashes(files, first)
    assert counted == []
    assert second == first


def test_same_size_new_mtime_rehashes(lib: Path, counted: list[Path]) -> None:
    files = _files(lib)
    first = compute_tracked_hashes(files, {})
    a = lib / "A.kicad_mod"
    data = bytearray(a.read_bytes())
    data[-2] = ord("X") if data[-2] != ord("X") else ord("Y")  # same size, other bytes
    st = a.stat()
    a.write_bytes(bytes(data))
    os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    counted.clear()
    second = compute_tracked_hashes(files, first)
    assert counted == [a]
    rel = files[0].rel
    assert second[rel].sha256 == sha256_file(a) != first[rel].sha256


def test_inode_change_rehashes(lib: Path, counted: list[Path]) -> None:
    files = _files(lib)
    first = compute_tracked_hashes(files, {})
    a = lib / "A.kicad_mod"
    st = a.stat()
    tmp = lib / "A.tmp"
    shutil.copyfile(a, tmp)  # same size and mtime: only the inode differs
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(tmp, a)
    assert a.stat().st_ino != st.st_ino
    counted.clear()
    second = compute_tracked_hashes(files, first)
    assert counted == [a]
    assert second[files[0].rel].sha256 == sha256_file(a)
    assert second[files[0].rel].inode == a.stat().st_ino


def test_stat_changes_during_hash_not_stored(lib: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _files(lib)
    a = lib / "A.kicad_mod"

    def touching(path: Path) -> str:
        digest = sha256_file(path)
        if path == a:
            _age(a, 100)  # a concurrent writer changes the stats mid-hash
        return digest

    monkeypatch.setattr(store_mod, "sha256_file", touching)
    rows = compute_tracked_hashes(files, {})
    assert rows[files[0].rel] == HashRow(sha256_file(a), None, None, None)
    assert rows[files[1].rel].size is not None


def test_racily_clean_not_stored(lib: Path, counted: list[Path]) -> None:
    files = _files(lib)
    os.utime(lib / "A.kicad_mod")  # mtime = now
    rows = compute_tracked_hashes(files, {})
    assert rows[files[0].rel].size is None and rows[files[0].rel].mtime_ns is None
    counted.clear()
    compute_tracked_hashes(files, rows)
    assert counted == [lib / "A.kicad_mod"]


def test_null_stats_rehash(lib: Path, counted: list[Path]) -> None:
    """Rows from before migration 0012 carry no stats: never reused."""
    files = _files(lib)
    previous = {f.rel: HashRow(sha256_file(f.path), None, None, None) for f in files}
    rows = compute_tracked_hashes(files, previous)
    assert sorted(counted) == sorted(f.path for f in files)
    assert all(r.size is not None for r in rows.values())


def test_project_files_always_hashed(tmp_path: Path, counted: list[Path]) -> None:
    project = copy_project(ECC83, tmp_path / "projects")
    for p in project.dir.rglob("*"):
        if p.is_file():
            _age(p)
    files = tracked_files(project, None)
    first = compute_tracked_hashes(files, {})
    counted.clear()
    second = compute_tracked_hashes(files, first)
    assert sorted(counted) == sorted(f.path for f in files)
    assert {k: v.sha256 for k, v in second.items()} == {k: v.sha256 for k, v in first.items()}


def test_vanished_file_skipped(lib: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _files(lib)
    a = lib / "A.kicad_mod"

    def vanishing(path: Path) -> str:
        if path == a:
            a.unlink()
            raise FileNotFoundError(path)
        return sha256_file(path)

    monkeypatch.setattr(store_mod, "sha256_file", vanishing)
    rows = compute_tracked_hashes(files, {})
    assert set(rows) == {files[1].rel}
    a_gone = compute_tracked_hashes(files, {})  # gone before the stat
    assert set(a_gone) == {files[1].rel}


async def test_repo_rows_round_trip(env: Env) -> None:
    repo = DocumentHashRepo(env.svc.db)
    rows = {
        "a.kicad_sch": HashRow("1" * 64, 10, 123456789, 42),
        "../lib/X.pretty/A.kicad_mod": HashRow("2" * 64, None, None, None),
    }
    await repo.replace("p1", rows)
    assert await repo.get_rows("p1") == rows
    assert await repo.get("p1") == {k: v.sha256 for k, v in rows.items()}
    await repo.replace("p1", {"b": "3" * 64})  # plain digests still accepted
    assert await repo.get_rows("p1") == {"b": HashRow("3" * 64, None, None, None)}


async def test_current_hashes_includes_library(env: Env, counted: list[Path]) -> None:
    project = env.project()
    lib = project.dir.parent / "lib" / "X.pretty"
    lib.mkdir(parents=True)
    shutil.copyfile(_FOOTPRINTS[0], lib / "A.kicad_mod")
    _age(lib / "A.kicad_mod")
    location = LibraryLocation(
        dir=lib, repo_root=project.dir.parent, table=project.dir / "fp-lib-table", nickname="X"
    )
    svc = env.svc
    # Default locator: no library, project files only.
    assert all(not k.startswith("../") for k in await svc.current_hashes(project))

    async def locate(p: Project) -> LibraryLocation | None:
        return location

    svc.locate_library = locate
    rows = await svc.current_hashes(project)
    rel = "../lib/X.pretty/A.kicad_mod"
    assert rows[rel].sha256 == sha256_file(lib / "A.kicad_mod")
    await svc.hashes.replace(project.id, rows)
    counted.clear()
    again = await svc.current_hashes(project)
    assert again[rel] == rows[rel]
    assert lib / "A.kicad_mod" not in counted
