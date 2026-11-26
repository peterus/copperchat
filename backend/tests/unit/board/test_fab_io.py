"""The file guards of the fab tool module (SP4 P8 review round 1): kicad-cli outputs read
back (`_read_small`), the upload zip (`zip_package`) and the 3D output check
(`_check_output`). Pure stdlib on `tmp_path`."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from copperchat.kicad_tools.board.fab import _check_output, _read_small, zip_package


def test_read_small_regular_file(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_bytes(b"\xef\xbb\xbfRef,Val\n")
    assert _read_small(tmp_path / "a.csv") == "Ref,Val\n"  # BOM stripped


def test_read_small_refuses_symlink_and_oversize(tmp_path: Path) -> None:
    (tmp_path / "real.csv").write_text("x")
    (tmp_path / "link.csv").symlink_to(tmp_path / "real.csv")
    with pytest.raises(OSError):
        _read_small(tmp_path / "link.csv")  # O_NOFOLLOW
    (tmp_path / "big.csv").write_text("x" * 101)
    with pytest.raises(ValueError):
        _read_small(tmp_path / "big.csv", max_bytes=100)
    (tmp_path / "dir.csv").mkdir()
    with pytest.raises((ValueError, OSError)):
        _read_small(tmp_path / "dir.csv")


def _outputs(tmp: Path) -> None:
    (tmp / "gerbers").mkdir()
    (tmp / "drill").mkdir()
    (tmp / "gerbers" / "b-F_Cu.gtl").write_text("G04*")
    (tmp / "gerbers" / "b-job.gbrjob").write_text("{}")
    (tmp / "drill" / "b-PTH.drl").write_text("M48")


def test_zip_package_flat_sorted_fixed_time(tmp_path: Path) -> None:
    _outputs(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("never zipped")
    (tmp_path / "gerbers" / "link.gbr").symlink_to(outside)
    zip_package(tmp_path, "b-jlcpcb.zip")
    with zipfile.ZipFile(tmp_path / "b-jlcpcb.zip") as zf:
        assert zf.namelist() == ["b-F_Cu.gtl", "b-PTH.drl", "b-job.gbrjob"]
        assert {i.date_time for i in zf.infolist()} == {(1980, 1, 1, 0, 0, 0)}
        assert all(i.compress_type == zipfile.ZIP_DEFLATED for i in zf.infolist())
    first = (tmp_path / "b-jlcpcb.zip").read_bytes()
    zip_package(tmp_path, "b-jlcpcb.zip")
    assert (tmp_path / "b-jlcpcb.zip").read_bytes() == first  # reproducible


def test_zip_package_duplicate_and_empty(tmp_path: Path) -> None:
    _outputs(tmp_path)
    (tmp_path / "drill" / "b-F_Cu.gtl").write_text("dup")
    with pytest.raises(ValueError, match="two outputs"):
        zip_package(tmp_path, "z.zip")
    empty = tmp_path / "empty"
    (empty / "gerbers").mkdir(parents=True)
    (empty / "drill").mkdir()
    with pytest.raises(ValueError, match="no gerber"):
        zip_package(empty, "z.zip")


def test_check_output(tmp_path: Path) -> None:
    step = tmp_path / "b.step"
    step.write_bytes(b"ISO-10303-21;\nHEADER;")
    assert _check_output(step, "step") == step.stat().st_size
    glb = tmp_path / "b.glb"
    glb.write_bytes(b"glTF\x02\x00\x00\x00")
    assert _check_output(glb, "glb") == 8
    (tmp_path / "empty.step").write_bytes(b"")
    with pytest.raises(ValueError, match="wrote no file"):
        _check_output(tmp_path / "empty.step", "step")
    (tmp_path / "bad.glb").write_bytes(b"<html>")
    with pytest.raises(ValueError, match="no GLB"):
        _check_output(tmp_path / "bad.glb", "glb")
    (tmp_path / "link.step").symlink_to(step)
    with pytest.raises(ValueError):
        _check_output(tmp_path / "link.step", "step")
    with pytest.raises(FileNotFoundError):
        _check_output(tmp_path / "missing.step", "step")
