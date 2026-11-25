"""`read.read_dru`: the defensive `.kicad_dru` read (B-C3) - pure, no KiCad."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from copperchat.board.model import board_hash
from copperchat.kicad_tools.board import read
from copperchat.kicad_tools.board.read import DRU_MAX_BYTES, read_dru
from tests.unit.board.builders import board


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records every path `read_dru` passes to `os.open`."""
    calls: list[str] = []
    real_open = os.open

    def spy(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        calls.append(str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(read.os, "open", spy)
    return calls


def test_dru_absent(tmp_path: Path) -> None:
    assert read_dru(tmp_path / "p.kicad_dru") == ("", "", ())


def test_dru_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "p.kicad_dru"
    path.write_text('(version 1)\n(rule "ü_rule")\n', encoding="utf-8")
    assert read_dru(path) == ('(version 1)\n(rule "ü_rule")\n', "", ())


def test_dru_symlink_ignored(tmp_path: Path, opened: list[str]) -> None:
    real = tmp_path / "elsewhere.kicad_dru"
    real.write_text('(version 1)\n(rule "secret")\n', encoding="utf-8")
    link = tmp_path / "p.kicad_dru"
    link.symlink_to(real)
    text, marker, warnings = read_dru(link)
    assert (text, marker) == ("", "")
    assert warnings == (".kicad_dru ignored: not a regular file",)
    assert opened == []  # never read through the link


@pytest.mark.timeout(5)
def test_dru_fifo_ignored(tmp_path: Path, opened: list[str]) -> None:
    path = tmp_path / "p.kicad_dru"
    os.mkfifo(path)
    assert read_dru(path) == ("", "", (".kicad_dru ignored: not a regular file",))
    assert opened == []


def test_dru_dir_ignored(tmp_path: Path) -> None:
    path = tmp_path / "p.kicad_dru"
    path.mkdir()
    assert read_dru(path) == ("", "", (".kicad_dru ignored: not a regular file",))


def test_dru_swapped_after_lstat_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A FIFO swapped in between `lstat` and `open`: the `fstat` check refuses it."""
    path = tmp_path / "p.kicad_dru"
    path.write_text("(version 1)\n", encoding="utf-8")
    real_lstat = os.lstat

    def swap(p: Any) -> os.stat_result:
        result = real_lstat(p)
        os.unlink(p)
        os.mkfifo(p)
        return result

    monkeypatch.setattr(read.os, "lstat", swap)
    assert read_dru(path) == ("", "", (".kicad_dru ignored: not a regular file",))


def test_dru_oversize(tmp_path: Path) -> None:
    path = tmp_path / "p.kicad_dru"
    path.write_bytes(b"#" * (DRU_MAX_BYTES + 1))
    text, marker, warnings = read_dru(path)
    st = os.stat(path)
    assert text == ""
    assert marker == f"oversize:{st.st_size}:{st.st_mtime_ns}"
    assert warnings == (".kicad_dru ignored: larger than 1 MiB",)

    with path.open("ab") as fh:
        fh.write(b"#")
    _, marker2, _ = read_dru(path)
    assert marker2 != marker
    base = board()
    assert board_hash(replace(base, dru_marker=marker)) != board_hash(
        replace(base, dru_marker=marker2)
    )


def test_dru_exactly_cap_is_read(tmp_path: Path) -> None:
    path = tmp_path / "p.kicad_dru"
    path.write_bytes(b"#" * DRU_MAX_BYTES)
    text, marker, warnings = read_dru(path)
    assert len(text) == DRU_MAX_BYTES and marker == "" and warnings == ()
