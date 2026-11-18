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


_SHIFT = 5_000_000_000  # copying/aging a file sets its ctime to now: look at it 5 s later


@pytest.fixture(autouse=True)
def later_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hashing clock runs 5 s ahead, so freshly aged files are not racily clean."""
    monkeypatch.setattr(store_mod, "_now_ns", lambda: time.time_ns() + _SHIFT)


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


def test_ctime_change_rehashes(lib: Path, counted: list[Path]) -> None:
    """An in-place same-size rewrite that restores the mtime: only the ctime tells."""
    files = _files(lib)
    first = compute_tracked_hashes(files, {})
    a = lib / "A.kicad_mod"
    st = a.stat()
    data = bytearray(a.read_bytes())
    data[-2] = ord("X") if data[-2] != ord("X") else ord("Y")  # same size, other bytes
    time.sleep(0.05)  # the ctime clock is coarse (kernel tick)
    with a.open("r+b") as fh:  # in place: same inode
        fh.write(bytes(data))
    os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns))
    after = a.stat()
    assert (after.st_size, after.st_mtime_ns, after.st_ino) == (
        st.st_size,
        st.st_mtime_ns,
        st.st_ino,
    )
    assert after.st_ctime_ns != st.st_ctime_ns
    counted.clear()
    second = compute_tracked_hashes(files, first)
    assert counted == [a]
    assert second[files[0].rel].sha256 == sha256_file(a) != first[files[0].rel].sha256
    assert second[files[0].rel].ctime_ns == after.st_ctime_ns


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
    assert rows[files[0].rel] == HashRow(sha256_file(a))
    assert rows[files[1].rel].size is not None


def test_racily_clean_not_stored(lib: Path, counted: list[Path]) -> None:
    files = _files(lib)
    now = store_mod._now_ns()
    os.utime(lib / "A.kicad_mod", ns=(now, now))  # mtime = now
    rows = compute_tracked_hashes(files, {})
    assert rows[files[0].rel].size is None and rows[files[0].rel].mtime_ns is None
    counted.clear()
    compute_tracked_hashes(files, rows)
    assert counted == [lib / "A.kicad_mod"]


def test_racily_clean_ctime_not_stored(
    lib: Path, counted: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old mtime does not make a file clean: its ctime (set by the aging utime just
    now) is in the reuse key too and still within RACY_NS."""
    monkeypatch.setattr(store_mod, "_now_ns", time.time_ns)
    files = _files(lib)
    a = (lib / "A.kicad_mod").stat()
    assert time.time_ns() - a.st_mtime_ns >= store_mod.RACY_NS
    assert time.time_ns() - a.st_ctime_ns < store_mod.RACY_NS
    rows = compute_tracked_hashes(files, {})
    assert rows[files[0].rel] == HashRow(sha256_file(lib / "A.kicad_mod"))
    counted.clear()
    compute_tracked_hashes(files, rows)
    assert sorted(counted) == sorted(f.path for f in files)


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
        "a.kicad_sch": HashRow("1" * 64, 10, 123456789, 42, 987654321),
        "../lib/X.pretty/A.kicad_mod": HashRow("2" * 64, None, None, None),
    }
    await repo.replace("p1", rows)
    assert await repo.get_rows("p1") == rows
    assert await repo.get("p1") == {k: v.sha256 for k, v in rows.items()}
    await repo.replace("p1", {"b": "3" * 64})  # plain digests still accepted
    assert await repo.get_rows("p1") == {"b": HashRow("3" * 64, None, None, None)}


async def test_repo_upsert_touches_only_given_rows(env: Env) -> None:
    repo = DocumentHashRepo(env.svc.db)
    await repo.replace("p1", {"a": "1" * 64, "b": "2" * 64, "c": "3" * 64})
    await repo.replace("p2", {"a": "4" * 64})
    await repo.upsert("p1", {"b": HashRow("5" * 64, 1, 2, 3, 4), "d": "6" * 64}, delete=["c"])
    assert await repo.get_rows("p1") == {
        "a": HashRow("1" * 64),
        "b": HashRow("5" * 64, 1, 2, 3, 4),
        "d": HashRow("6" * 64),
    }
    assert await repo.get("p2") == {"a": "4" * 64}
    await repo.upsert("p1", {}, delete=["a", "b", "d"])
    assert await repo.get("p1") == {}


async def test_repo_replace_many_rows(env: Env) -> None:
    repo = DocumentHashRepo(env.svc.db)
    rows = {f"f{i:05}.kicad_mod": HashRow(f"{i:064x}", i, i, i, i) for i in range(3000)}
    await repo.replace("p1", rows)
    assert await repo.get_rows("p1") == rows


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
