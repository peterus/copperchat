"""Safe fab output paths and the two-step swap (SP4 P8 Task 1, `cc/board/paths.py`)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from copperchat.board import paths
from copperchat.board.paths import (
    replace_file,
    safe_output_dir,
    staging_dir,
    swap_in,
    sweep_stale,
)
from copperchat.errors import StructuredToolError

CALL = "0123456789ab"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    return project


def _package(d: Path, text: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "fab-report.md").write_text(text)
    (d / "gerbers").mkdir(exist_ok=True)
    (d / "gerbers" / "board-F_Cu.gtl").write_bytes(text.encode() * 3)
    return d


def _tree(d: Path) -> dict[str, bytes]:
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def _unsafe(exc: pytest.ExceptionInfo[StructuredToolError]) -> str:
    assert exc.value.code == "output_path_unsafe", exc.value.to_dict()
    assert exc.value.details is not None
    reason: str = exc.value.details["reason"]
    return reason


def test_creates_fab(tmp_path: Path) -> None:
    project = _project(tmp_path)
    target = safe_output_dir(project, "fab/jlcpcb")
    assert target == project / "fab" / "jlcpcb"
    assert (project / "fab").is_dir() and not (project / "fab").is_symlink()
    assert not target.exists()  # the target itself is created by the swap


def test_existing_target_returned(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _package(project / "fab" / "jlcpcb", "old")
    assert safe_output_dir(project, "fab/jlcpcb") == project / "fab" / "jlcpcb"


def test_symlinked_fab_refused(tmp_path: Path) -> None:
    project = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "fab").symlink_to(outside)
    with pytest.raises(StructuredToolError) as exc:
        safe_output_dir(project, "fab/jlcpcb")
    assert _unsafe(exc) == "symlink"
    assert list(outside.iterdir()) == []


def test_symlinked_target_refused(tmp_path: Path) -> None:
    project = _project(tmp_path)
    other = project / "fab" / "other"
    other.mkdir(parents=True)
    (project / "fab" / "jlcpcb").symlink_to(other)
    with pytest.raises(StructuredToolError) as exc:
        safe_output_dir(project, "fab/jlcpcb")
    assert _unsafe(exc) == "symlink"


def test_outside_refused(tmp_path: Path) -> None:
    project = _project(tmp_path)
    for rel in ("../x", "/abs/x", "fab/../../x", "", "fab//x", "fab/./x"):
        with pytest.raises(StructuredToolError) as exc:
            safe_output_dir(project, rel)
        assert _unsafe(exc) == "outside", rel
    assert not (tmp_path / "x").exists()


def test_fab_is_file_refused(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "fab").write_text("not a directory")
    with pytest.raises(StructuredToolError) as exc:
        safe_output_dir(project, "fab/jlcpcb")
    assert _unsafe(exc) == "not_a_dir"


def test_target_is_file_refused(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "fab").mkdir()
    (project / "fab" / "jlcpcb").write_text("x")
    with pytest.raises(StructuredToolError) as exc:
        safe_output_dir(project, "fab/jlcpcb")
    assert _unsafe(exc) == "not_a_dir"


def test_staging_dir_fresh(tmp_path: Path) -> None:
    project = _project(tmp_path)
    target = safe_output_dir(project, "fab/jlcpcb")
    tmp = staging_dir(target, CALL)
    assert tmp == project / "fab" / f".jlcpcb.tmp-{CALL}"
    assert tmp.is_dir() and list(tmp.iterdir()) == []
    with pytest.raises(FileExistsError):
        staging_dir(target, CALL)  # never reuses (or writes into) an existing dir


def test_swap_fresh(tmp_path: Path) -> None:
    project = _project(tmp_path)
    target = safe_output_dir(project, "fab/jlcpcb")
    new = _package(staging_dir(target, CALL), "new")
    warning = swap_in(new, target, CALL)
    assert warning is None
    assert (target / "fab-report.md").read_text() == "new"
    assert sorted(p.name for p in (project / "fab").iterdir()) == ["jlcpcb"]


def test_swap_replaces_and_removes_old(tmp_path: Path) -> None:
    project = _project(tmp_path)
    target = safe_output_dir(project, "fab/jlcpcb")
    _package(target, "old")
    new = _package(staging_dir(target, CALL), "new")
    assert swap_in(new, target, CALL) is None
    assert (target / "fab-report.md").read_text() == "new"
    assert sorted(p.name for p in (project / "fab").iterdir()) == ["jlcpcb"]


def test_second_rename_failure_restores_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    target = safe_output_dir(project, "fab/jlcpcb")
    _package(target, "old")
    before = _tree(target)
    new = _package(staging_dir(target, CALL), "new")
    real = os.rename
    calls: list[tuple[Any, Any]] = []

    def flaky(src: Any, dst: Any) -> None:
        calls.append((src, dst))
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        real(src, dst)

    monkeypatch.setattr(paths.os, "rename", flaky)
    with pytest.raises(StructuredToolError) as exc:
        swap_in(new, target, CALL)
    assert exc.value.code == "export_failed"
    assert _tree(target) == before  # the old package is back, byte-identical
    assert not (project / "fab" / f"jlcpcb.old-{CALL}").exists()
    assert len(calls) == 3  # old aside, new in (failed), old back


def test_rmtree_old_failure_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path)
    target = safe_output_dir(project, "fab/jlcpcb")
    _package(target, "old")
    new = _package(staging_dir(target, CALL), "new")

    def boom(path: Any, *args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(paths.shutil, "rmtree", boom)
    warning = swap_in(new, target, CALL)
    assert warning is not None and f"jlcpcb.old-{CALL}" in warning
    assert (target / "fab-report.md").read_text() == "new"  # the export still succeeded


def test_never_touches_outside_fab(tmp_path: Path) -> None:
    project = _project(tmp_path)
    elsewhere = project / "docs" / "jlcpcb"
    _package(elsewhere, "keep")
    new = _package(project / "docs" / f".jlcpcb.tmp-{CALL}", "new")
    with pytest.raises(StructuredToolError) as exc:
        swap_in(new, elsewhere, CALL)
    assert _unsafe(exc) == "outside"
    assert (elsewhere / "fab-report.md").read_text() == "keep"
    with pytest.raises(StructuredToolError):
        replace_file(new / "fab-report.md", elsewhere / "fab-report.md")
    assert (elsewhere / "fab-report.md").read_text() == "keep"
    with pytest.raises(StructuredToolError):
        sweep_stale(project / "docs", "jlcpcb")


def test_guard_refuses_symlinked_fab_at_swap(tmp_path: Path) -> None:
    """Defence in depth: `fab` replaced by a symlink after validation → no rename."""
    project = _project(tmp_path)
    outside = tmp_path / "outside"
    _package(outside / "jlcpcb", "outside")
    new = _package(tmp_path / "new", "new")
    (project / "fab").symlink_to(outside)
    with pytest.raises(StructuredToolError):
        swap_in(new, project / "fab" / "jlcpcb", CALL)
    assert (outside / "jlcpcb" / "fab-report.md").read_text() == "outside"


def test_replace_file(tmp_path: Path) -> None:
    project = _project(tmp_path)
    target_dir = safe_output_dir(project, "fab/3d")
    target_dir.mkdir()
    (target_dir / "b.step").write_text("old")
    stage = staging_dir(target_dir, CALL)
    (stage / "b.step").write_text("new")
    replace_file(stage / "b.step", target_dir / "b.step")
    assert (target_dir / "b.step").read_text() == "new"


def test_sweep_removes_leftovers(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fab = project / "fab"
    _package(fab / f".jlcpcb.tmp-{CALL}", "crashed")
    _package(fab / f"jlcpcb.old-{CALL}", "crashed")
    _package(fab / "jlcpcb", "current")
    removed = sweep_stale(fab, "jlcpcb")
    assert removed == [f".jlcpcb.tmp-{CALL}", f"jlcpcb.old-{CALL}"]
    assert sorted(p.name for p in fab.iterdir()) == ["jlcpcb"]


def test_sweep_skips_symlink_and_others(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fab = project / "fab"
    fab.mkdir()
    outside = _package(tmp_path / "outside", "precious")
    (fab / "jlcpcb.old-aaaaaaaaaaaa").symlink_to(outside)
    _package(fab / "jlcpcb.old-notes", "keep")
    (fab / ".jlcpcb.tmp-bbbbbbbbbbbb").write_text("a regular file")
    _package(fab / "3d.old-cccccccccccc", "other target")
    _package(fab / "jlcpcb.old-ABCDEF012345", "upper case is not ours")
    assert sweep_stale(fab, "jlcpcb") == []
    assert (outside / "fab-report.md").read_text() == "precious"
    assert (fab / "jlcpcb.old-aaaaaaaaaaaa").is_symlink()
    assert (fab / "jlcpcb.old-notes" / "fab-report.md").exists()
    assert (fab / ".jlcpcb.tmp-bbbbbbbbbbbb").is_file()
    assert (fab / "3d.old-cccccccccccc").is_dir()
    assert (fab / "jlcpcb.old-ABCDEF012345").is_dir()


def test_symlinked_fab_not_swept(tmp_path: Path) -> None:
    project = _project(tmp_path)
    outside = tmp_path / "outside"
    _package(outside / f"jlcpcb.old-{CALL}", "precious")
    (project / "fab").symlink_to(outside)
    with pytest.raises(StructuredToolError) as exc:
        safe_output_dir(project, "fab/jlcpcb")
    assert _unsafe(exc) == "symlink"
    with pytest.raises(StructuredToolError) as exc2:
        sweep_stale(project / "fab", "jlcpcb")
    assert _unsafe(exc2) == "symlink"
    assert (outside / f"jlcpcb.old-{CALL}" / "fab-report.md").read_text() == "precious"


def test_new_call_id() -> None:
    ids = {paths.new_call_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) == 12 and int(i, 16) >= 0 for i in ids)
