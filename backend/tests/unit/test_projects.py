import hashlib
from pathlib import Path

import pytest

from copperchat.errors import InvalidProjectId, ProjectNotFound
from copperchat.projects import (
    agent_dir,
    discover_projects,
    get_project,
    lock_status,
    project_id_for,
)


def marker(root: Path, rel: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def test_discovers_at_any_depth_with_files(tmp_path: Path) -> None:
    marker(tmp_path, "a/b/c/amp.kicad_pro")
    marker(tmp_path, "a/b/c/amp.kicad_sch")
    marker(tmp_path, "top.kicad_pro")
    marker(tmp_path, "top.kicad_pcb")
    projects = discover_projects(tmp_path)
    assert [p.rel_path for p in projects] == ["a/b/c/amp.kicad_pro", "top.kicad_pro"]
    amp, top = projects
    assert amp.name == "amp" and amp.sch_file is not None and amp.pcb_file is None
    assert top.pcb_file is not None and top.sch_file is None
    assert amp.dir == (tmp_path / "a/b/c").resolve()
    assert amp.id == hashlib.sha1(b"a/b/c/amp.kicad_pro").hexdigest()[:12]


def test_skips_hidden_agent_and_symlinked_dirs(tmp_path: Path) -> None:
    root = tmp_path / "root"
    marker(root, ".git/x.kicad_pro")
    marker(root, "p/.kicad-agent/view/p.kicad_pro")
    marker(root, ".hidden/y.kicad_pro")
    outside = tmp_path / "outside"
    marker(outside, "evil.kicad_pro")
    (root / "link").symlink_to(outside, target_is_directory=True)
    assert discover_projects(root) == []


def test_id_is_stable_posix_hash() -> None:
    assert project_id_for("x/y.kicad_pro") == hashlib.sha1(b"x/y.kicad_pro").hexdigest()[:12]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "../../etc/passwd",
        "..",
        "ABCDEF123456",
        "abc",
        "a1b2c3d4e5f6/..",
        "a1b2c3d4e5f6a",
        "a1b2c3d4e5f%",
        "a1b2c3d4e5f6\x00",
        "/etc/passwd",
    ],
)
def test_invalid_ids_rejected_before_touching_fs(tmp_path: Path, bad: str) -> None:
    with pytest.raises(InvalidProjectId):
        get_project(tmp_path, bad)


def test_unknown_id(tmp_path: Path) -> None:
    with pytest.raises(ProjectNotFound):
        get_project(tmp_path, "0123456789ab")


def test_lock_status(tmp_path: Path) -> None:
    marker(tmp_path, "p/demo.kicad_pro")
    marker(tmp_path, "p/demo.kicad_sch")
    marker(tmp_path, "p/demo.kicad_pcb")
    project = discover_projects(tmp_path)[0]
    assert lock_status(project).locked is False
    lock = marker(tmp_path, "p/~demo.kicad_sch.lck")
    status = lock_status(project)
    assert status.locked is True
    assert status.lock_files == (lock.resolve(),)
    assert agent_dir(project) == project.dir / ".kicad-agent"


def test_project_lock_file_counts(tmp_path: Path) -> None:
    # Spike 1C: KiCad 11 writes only ~STEM.kicad_pro.lck for the whole project.
    marker(tmp_path, "p/demo.kicad_pro")
    project = discover_projects(tmp_path)[0]
    lock = marker(tmp_path, "p/~demo.kicad_pro.lck")
    assert lock_status(project).lock_files == (lock.resolve(),)
