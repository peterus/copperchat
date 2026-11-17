"""tracked_files: project files, the project's fp-lib-table and the local library (FP-C6)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from copperchat.history.library_lock import assert_library_locked, library_lock
from copperchat.history.store import LibraryLocation, TrackedFile, document_files, tracked_files
from copperchat.projects import Project, discover_projects
from tests.support.history import ECC83, HIERARCHY

# KiCad-saved footprints, copied (never hand-written) into the test library.
_FOOTPRINTS = sorted((ECC83 / "footprints.pretty").glob("*.kicad_mod"))


def _project_at(repo: Path, rel: str, src: Path = ECC83) -> Project:
    dest = repo / rel if rel else repo
    shutil.copytree(src, dest, dirs_exist_ok=True)
    return next(p for p in discover_projects(repo) if p.dir == dest.resolve())


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo.resolve()


def _library(repo: Path, project: Project, *, create: bool = True) -> LibraryLocation:
    lib = repo / "footprints" / "X.pretty"
    if create:
        lib.mkdir(parents=True)
        shutil.copyfile(_FOOTPRINTS[0], lib / "A.kicad_mod")
        shutil.copyfile(_FOOTPRINTS[1], lib / "B.kicad_mod")
    return LibraryLocation(
        dir=lib, repo_root=repo, table=project.dir / "fp-lib-table", nickname="X"
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _repo(tmp_path)


@pytest.fixture
def main(repo: Path) -> Project:
    project = _project_at(repo, "hardware/main")
    _project_at(repo, "hardware/io", HIERARCHY)
    return project


def _library_entries(files: list[TrackedFile]) -> list[TrackedFile]:
    return [f for f in files if f.library]


def files_paths(files: list[TrackedFile]) -> list[Path]:
    return [f.path for f in files]


def test_project_files_unchanged(main: Project) -> None:
    files = tracked_files(main, None)
    assert [f.path for f in files] == document_files(main)
    assert [f.rel for f in files] == [
        p.relative_to(main.dir).as_posix() for p in document_files(main)
    ]
    assert not any(f.library for f in files)


def test_table_and_library(repo: Path, main: Project) -> None:
    assert (main.dir / "fp-lib-table").is_file()  # KiCad-saved table of the fixture
    files = tracked_files(main, _library(repo, main))
    project_part = files[: len(document_files(main))]
    assert files_paths(project_part) == document_files(main)
    table = files[len(project_part)]
    assert table == TrackedFile(path=main.dir / "fp-lib-table", rel="fp-lib-table", library=False)
    lib = _library_entries(files)
    assert [f.rel for f in lib] == [
        "../../footprints/X.pretty/A.kicad_mod",
        "../../footprints/X.pretty/B.kicad_mod",
    ]
    assert files[len(project_part) + 1 :] == lib


def test_library_dir_missing(repo: Path, main: Project) -> None:
    files = tracked_files(main, _library(repo, main, create=False))
    assert _library_entries(files) == []
    assert [f.rel for f in files][-1] == "fp-lib-table"


def test_symlinks_skipped(tmp_path: Path, repo: Path, main: Project) -> None:
    location = _library(repo, main)
    (location.dir / "C.kicad_mod").symlink_to(location.dir / "A.kicad_mod")
    rels = [f.rel for f in _library_entries(tracked_files(main, location))]
    assert "../../footprints/X.pretty/C.kicad_mod" not in rels
    assert len(rels) == 2

    real = tmp_path / "elsewhere.pretty"
    real.mkdir()
    shutil.copyfile(_FOOTPRINTS[0], real / "A.kicad_mod")
    linked = repo / "footprints" / "Y.pretty"
    linked.symlink_to(real, target_is_directory=True)
    loc = LibraryLocation(dir=linked, repo_root=repo, table=location.table, nickname="Y")
    assert _library_entries(tracked_files(main, loc)) == []


def test_never_outside_root(tmp_path: Path, repo: Path, main: Project) -> None:
    outside = tmp_path / "outside" / "X.pretty"
    outside.mkdir(parents=True)
    loc = LibraryLocation(
        dir=outside, repo_root=repo, table=main.dir / "fp-lib-table", nickname="X"
    )
    with pytest.raises(ValueError):
        tracked_files(main, loc)


def test_project_at_root(repo: Path) -> None:
    project = _project_at(repo, "")
    files = tracked_files(project, _library(repo, project))
    lib = _library_entries(files)
    assert [f.rel for f in lib] == [
        "footprints/X.pretty/A.kicad_mod",
        "footprints/X.pretty/B.kicad_mod",
    ]
    assert all(f.library for f in lib)


async def test_library_lock_identity(repo: Path) -> None:
    lib = repo / "footprints" / "X.pretty"
    lock = library_lock(lib)
    assert library_lock(repo / "footprints" / ".." / "footprints" / "X.pretty") is lock
    assert library_lock(repo / "footprints" / "Y.pretty") is not lock
    with pytest.raises(AssertionError):
        assert_library_locked(lib)
    async with lock:
        assert_library_locked(lib)
    with pytest.raises(AssertionError):
        assert_library_locked(lib)
