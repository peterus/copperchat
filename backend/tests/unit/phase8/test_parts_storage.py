from pathlib import Path

import pytest

from copperchat import parts
from copperchat.projects import Project


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("TPS62133", "TPS62133"),
        (" LM358/NOPB ", "LM358_NOPB"),
        ("ESP32-S3-WROOM-1 N8R8", "ESP32-S3-WROOM-1_N8R8"),
        ("BAT54C.215", "BAT54C.215"),
        ("LM358\\X", "LM358_X"),
    ],
)
def test_mpn_dirname_ok(raw: str, expected: str) -> None:
    assert parts.mpn_dirname(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "..",
        "../etc",
        "a/../b",
        ".hidden",
        "_uploads",
        "x" * 65,
        "a\x00b",
        "ä-part",
        "a;rm",
        "a..b",
        "/",
        " ",
    ],
)
def test_mpn_dirname_rejects(bad: str) -> None:
    with pytest.raises(parts.InvalidMpn):
        parts.mpn_dirname(bad)


def test_user_notes_roundtrip_sets_flag(project: Project) -> None:
    parts.write_user_notes(project, "TPS62133", "# Notes\nmine\n")
    text = parts.read_notes(project, "TPS62133")
    assert text.startswith("---\n") and "user_edited: true" in text and "mine" in text
    parts.write_user_notes(
        project, "TPS62133", "---\nmpn: TPS62133\nuser_edited: false\n---\nedited\n"
    )
    text = parts.read_notes(project, "TPS62133")
    assert "user_edited: true" in text and "user_edited: false" not in text
    assert "mpn: TPS62133" in text
    assert parts.list_parts(project)[0].user_edited


def test_too_large_notes_rejected(project: Project) -> None:
    with pytest.raises(parts.InvalidMpn):
        parts.write_user_notes(project, "X", "x" * (parts.MAX_NOTES_BYTES + 1))


def test_list_parts_skips_uploads_and_symlinks(project: Project, tmp_path: Path) -> None:
    root = project.dir / "docs" / "parts"
    (root / "_uploads").mkdir(parents=True)
    (root / "LM358").mkdir()
    (root / "LM358" / "datasheet.pdf").write_bytes(b"%PDF-1.7\n")
    (root / "evil").symlink_to(tmp_path, target_is_directory=True)
    (root / "file.md").write_text("x")
    infos = parts.list_parts(project)
    assert [p.mpn for p in infos] == ["LM358"]
    assert infos[0].has_datasheet and not infos[0].has_notes and infos[0].updated_at
    assert parts.datasheet_path(project, "LM358") == root / "LM358" / "datasheet.pdf"
    assert parts.datasheet_path(project, "evil") is None


def test_symlinked_files_are_not_served(project: Project, tmp_path: Path) -> None:
    d = project.dir / "docs" / "parts" / "X"
    d.mkdir(parents=True)
    (tmp_path / "secret").write_text("secret")
    (d / "notes.md").symlink_to(tmp_path / "secret")
    (d / "datasheet.pdf").symlink_to(tmp_path / "secret")
    assert parts.read_notes(project, "X") == ""
    assert parts.datasheet_path(project, "X") is None
    with pytest.raises(parts.InvalidMpn):
        parts.write_user_notes(project, "X", "mine")
    assert (tmp_path / "secret").read_text() == "secret"


def test_parts_root_symlinked_outside(project: Project, tmp_path: Path) -> None:
    outside = tmp_path / "out"
    (outside / "A").mkdir(parents=True)
    (project.dir / "docs").mkdir()
    (project.dir / "docs" / "parts").symlink_to(outside, target_is_directory=True)
    assert parts.list_parts(project) == []
    with pytest.raises(parts.InvalidMpn):
        parts.write_user_notes(project, "A", "x")
    assert not (outside / "A" / "notes.md").exists()
