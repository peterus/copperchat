"""Sheet file name validation (spec §10, E7 Task 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.edit.sheets import validate_sheet_file, validate_sheet_name
from copperchat.projects import Project


@pytest.fixture
def project(tmp_path: Path) -> Project:
    d = tmp_path / "proj"
    d.mkdir()
    (d / "proj.kicad_pro").write_text("{}")
    (d / "proj.kicad_sch").write_text("")
    return Project(
        "id", "proj", "proj/proj.kicad_pro", d, d / "proj.kicad_pro", d / "proj.kicad_sch", None
    )


@pytest.mark.parametrize("name", ["power.kicad_sch", "sub/power.kicad_sch", "a-b_c.1.kicad_sch"])
def test_valid_names(project: Project, name: str) -> None:
    assert validate_sheet_file(project, name, existing=set()) == name


@pytest.mark.parametrize(
    "name",
    [
        "../x.kicad_sch",
        "sub/../x.kicad_sch",
        "/abs.kicad_sch",
        "a/b/c.kicad_sch",  # two directories
        "x.sch",
        "x",
        ".kicad_sch",
        "",
        "a\\b.kicad_sch",
        "x\x00.kicad_sch",
        "x y;rm.kicad_sch",
        "./x.kicad_sch",
        "sub//x.kicad_sch",
        "x.kicad_sch/",
        "proj.kicad_sch",  # the root file, even though it exists in the hierarchy
    ],
)
def test_rejected_names(project: Project, name: str) -> None:
    with pytest.raises(StructuredToolError) as err:
        validate_sheet_file(project, name, existing={"other.kicad_sch"})
    assert err.value.code == "invalid_argument"


def test_non_string_rejected(project: Project) -> None:
    with pytest.raises(StructuredToolError):
        validate_sheet_file(project, 5, existing=set())  # type: ignore[arg-type]


def test_overlong_rejected(project: Project) -> None:
    with pytest.raises(StructuredToolError):
        validate_sheet_file(project, "a" * 200 + ".kicad_sch", existing=set())


def test_existing_file_outside_hierarchy_rejected(project: Project) -> None:
    (project.dir / "stray.kicad_sch").write_text("")
    with pytest.raises(StructuredToolError) as err:
        validate_sheet_file(project, "stray.kicad_sch", existing=set())
    assert "exists" in err.value.why
    # case-insensitive clash is refused as well
    with pytest.raises(StructuredToolError):
        validate_sheet_file(project, "STRAY.kicad_sch", existing=set())


def test_existing_hierarchy_file_allowed(project: Project) -> None:
    (project.dir / "power.kicad_sch").write_text("")
    assert validate_sheet_file(project, "power.kicad_sch", existing={"power.kicad_sch"}) == (
        "power.kicad_sch"
    )


def test_symlinked_directory_rejected(project: Project, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (project.dir / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(StructuredToolError) as err:
        validate_sheet_file(project, "link/x.kicad_sch", existing=set())
    assert "symbolic link" in err.value.why


def test_symlinked_file_rejected(project: Project, tmp_path: Path) -> None:
    target = tmp_path / "t.kicad_sch"
    target.write_text("")
    (project.dir / "l.kicad_sch").symlink_to(target)
    with pytest.raises(StructuredToolError):
        validate_sheet_file(project, "l.kicad_sch", existing={"l.kicad_sch"})


def test_subdirectory_is_not_created_by_validation(project: Project) -> None:
    validate_sheet_file(project, "newdir/x.kicad_sch", existing=set())
    assert not (project.dir / "newdir").exists()


def test_sheet_name_rules() -> None:
    assert validate_sheet_name("Power 5V") == "Power 5V"
    for bad in ["", "  ", "a/b", "x" * 100, "tab\tname", " lead"]:
        with pytest.raises(StructuredToolError):
            validate_sheet_name(bad)
