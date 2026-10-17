import io
from pathlib import Path

import pytest
from PIL import Image

from copperchat import uploads
from copperchat.projects import Project


def png(w: int, h: int, mode: str = "RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, (w, h), (200, 50, 50) if mode == "RGB" else (200, 50, 50, 128)).save(buf, "PNG")
    return buf.getvalue()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\x\\board photo.JPG", "board_photo.JPG"),
        ("..", "upload"),
        (".bashrc", "bashrc"),
        (None, "upload"),
        ("a\x00b.pdf", "a_b.pdf"),
        ("x" * 300 + ".pdf", "x" * 96 + ".pdf"),
        ("Schaltplan-Übersicht.pdf", "Schaltplan-Ubersicht.pdf"),
        ('"><script>.png', "script_.png"),
    ],
)
def test_safe_filename(raw: str | None, expected: str) -> None:
    assert uploads.safe_filename(raw) == expected


def test_sniff() -> None:
    assert uploads.sniff(b"%PDF-1.7\n") == ("pdf", "application/pdf")
    assert uploads.sniff(png(1, 1)[:16]) == ("image", "image/png")
    assert uploads.sniff(b"\xff\xd8\xff\xe0abc") == ("image", "image/jpeg")
    assert uploads.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == ("image", "image/webp")
    assert uploads.sniff(b"GIF89a....") == ("image", "image/gif")
    assert uploads.sniff(b"<html>") is None
    assert uploads.sniff(b"<svg xmlns=") is None
    assert uploads.sniff(b"\x00\x00\x00\x18ftypheic") is None


def test_pdf_stored_under_parts_uploads(project: Project) -> None:
    up = uploads.store_upload(project, b"%PDF-1.7\nbody", "../TPS62133 datasheet.pdf")
    assert up.kind == "pdf" and up.name == "TPS62133_datasheet.pdf"
    assert up.rel_path == f"docs/parts/_uploads/{up.id[:8]}-TPS62133_datasheet.pdf"
    assert (project.dir / up.rel_path).read_bytes() == b"%PDF-1.7\nbody"
    other = uploads.store_upload(project, b"%PDF-1.7\n", "notes")
    assert other.name == "notes.pdf"


def test_image_reencoded_and_downscaled(project: Project) -> None:
    up = uploads.store_upload(project, png(4000, 1000), "photo.png")
    assert up.kind == "image" and up.rel_path.startswith(".kicad-agent/uploads/")
    assert up.rel_path == f".kicad-agent/uploads/{up.id}.jpg" and up.mime == "image/jpeg"
    with Image.open(project.dir / up.rel_path) as im:
        assert max(im.size) == uploads.MAX_IMAGE_EDGE


def test_transparent_image_stays_png(project: Project) -> None:
    up = uploads.store_upload(project, png(10, 10, "RGBA"), "logo.png")
    assert up.mime == "image/png" and up.rel_path.endswith(".png")


def test_rejections(project: Project) -> None:
    with pytest.raises(uploads.UploadRejected) as e:
        uploads.store_upload(project, b"#!/bin/sh\n", "x.pdf")
    assert e.value.status == 415
    with pytest.raises(uploads.UploadRejected) as e:
        uploads.store_upload(project, b"%PDF-" + b"0" * uploads.MAX_PDF_BYTES, "big.pdf")
    assert e.value.status == 413
    with pytest.raises(uploads.UploadRejected) as e:
        uploads.store_upload(project, b"\x89PNG\r\n\x1a\nbroken", "x.png")
    assert e.value.status == 415


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_pixel_bomb_rejected(project: Project) -> None:
    buf = io.BytesIO()
    Image.new("1", (10000, 10000)).save(buf, "PNG")  # tiny file, 100 MP
    with pytest.raises(uploads.UploadRejected) as e:
        uploads.store_upload(project, buf.getvalue(), "bomb.png")
    assert e.value.status == 413


def test_parts_dir_symlinked_outside_rejected(project: Project, tmp_path: Path) -> None:
    (project.dir / "docs").mkdir()
    (project.dir / "docs" / "parts").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(uploads.UploadRejected) as e:
        uploads.store_upload(project, b"%PDF-1.7\n", "a.pdf")
    assert e.value.status == 400


def test_agent_dir_symlinked_outside_rejected(project: Project, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (project.dir / ".kicad-agent").symlink_to(outside, target_is_directory=True)
    with pytest.raises(uploads.UploadRejected):
        uploads.store_upload(project, png(4, 4), "a.png")
    assert list(outside.iterdir()) == []
