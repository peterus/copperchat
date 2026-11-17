"""Local footprint library derivation (pure, no KiCad)."""

from __future__ import annotations

from pathlib import Path

from copperchat.library.local import NAME_RE, base_nickname, derive_local_library


def test_nested_project_uri(tmp_path: Path) -> None:
    root = tmp_path / "OpenDriveHub"
    lib = derive_local_library(root / "hardware" / "main", root, set())
    assert lib.nickname == "OpenDriveHub"
    assert lib.dir == root / "footprints/OpenDriveHub.pretty"
    assert lib.uri == "${KIPRJMOD}/../../footprints/OpenDriveHub.pretty"
    assert lib.repo_root == root
    assert lib.table == root / "hardware" / "main" / "fp-lib-table"


def test_project_at_root_uri(tmp_path: Path) -> None:
    root = tmp_path / "OpenDriveHub"
    lib = derive_local_library(root, root, set())
    assert lib.uri == "${KIPRJMOD}/footprints/OpenDriveHub.pretty"


def test_sanitise_and_cut(tmp_path: Path) -> None:
    assert base_nickname(tmp_path / "my repo.ü-x") == "my_repo__-x"
    name = "a" * 20 + "b" * 20
    assert base_nickname(tmp_path / name) == name[:32]


def test_global_collision_appends_local(tmp_path: Path) -> None:
    root = tmp_path / "OpenDriveHub"
    lib = derive_local_library(root, root, {"OpenDriveHub"})
    assert lib.nickname == "OpenDriveHub_local"
    assert lib.dir == root / "footprints/OpenDriveHub_local.pretty"
    assert lib.uri.endswith("/footprints/OpenDriveHub_local.pretty")


def test_same_result_for_two_projects(tmp_path: Path) -> None:
    root = tmp_path / "OpenDriveHub"
    a = derive_local_library(root / "hardware/main", root, set())
    b = derive_local_library(root / "hardware/io", root, set())
    assert (a.nickname, a.dir, a.uri) == (b.nickname, b.dir, b.uri)
    c = derive_local_library(root / "main", root, set())
    assert c.nickname == a.nickname and c.dir == a.dir and c.uri != a.uri


def test_name_re() -> None:
    for ok in ("L_Bourns_SRN3012BTA", "R_0805+x.1", "a"):
        assert NAME_RE.match(ok)
    for bad in ("", "-x", "a/b", "..", "a b", "a" * 101):
        assert not NAME_RE.match(bad)
