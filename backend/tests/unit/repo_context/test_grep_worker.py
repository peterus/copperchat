from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


def run_worker(root: Path, files: list[str], query: str, **overrides: Any) -> list[dict[str, Any]]:
    request = {
        "root": str(root),
        "files": files,
        "query": query,
        "regex": False,
        "case_sensitive": False,
        "max_results": 50,
        "max_bytes": 8 * 1024 * 1024,
    } | overrides
    proc = subprocess.run(
        [sys.executable, "-P", "-m", "copperchat.repo_context.grep_worker"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return [json.loads(line) for line in proc.stdout.splitlines()]


def matches(lines: list[dict[str, Any]]) -> list[tuple[str, int, str]]:
    return [(m["path"], m["line"], m["text"]) for m in lines if "line" in m]


def done(lines: list[dict[str, Any]]) -> dict[str, Any]:
    assert lines[-1].get("done") is True
    return lines[-1]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("alpha\nBeta\nbeta\n")
    (root / "sub" / "b.txt").write_text("beta gamma\n")
    return root


def test_worker_literal_matches_and_done(root: Path) -> None:
    lines = run_worker(root, ["a.txt", "sub/b.txt"], "beta")
    assert matches(lines) == [
        ("a.txt", 2, "Beta"),
        ("a.txt", 3, "beta"),
        ("sub/b.txt", 1, "beta gamma"),
    ]
    assert done(lines) == {"done": True, "files_scanned": 2, "stopped_by": None}


def test_worker_case_sensitive_and_regex(root: Path) -> None:
    lines = run_worker(root, ["a.txt"], "Beta", case_sensitive=True)
    assert matches(lines) == [("a.txt", 2, "Beta")]
    lines = run_worker(root, ["a.txt", "sub/b.txt"], r"^be.a$", regex=True)
    assert matches(lines) == [("a.txt", 2, "Beta"), ("a.txt", 3, "beta")]


def test_worker_max_results(root: Path) -> None:
    lines = run_worker(root, ["a.txt", "sub/b.txt"], "beta", max_results=2)
    assert len(matches(lines)) == 2
    assert done(lines)["stopped_by"] == "max_results"


def test_worker_bytes_budget(root: Path) -> None:
    lines = run_worker(root, ["a.txt", "sub/b.txt"], "beta", max_bytes=8)
    assert matches(lines) == []
    assert done(lines)["stopped_by"] == "bytes"


def test_worker_skips_binary(root: Path) -> None:
    (root / "blob.bin").write_bytes(b"beta\x00\x01")
    lines = run_worker(root, ["blob.bin", "a.txt"], "beta")
    assert {m[0] for m in matches(lines)} == {"a.txt"}
    assert done(lines)["files_scanned"] == 1


def test_worker_refuses_paths_outside_root(root: Path, tmp_path: Path) -> None:
    (tmp_path / "outside.txt").write_text("beta\n")
    (root / "link.txt").symlink_to(tmp_path / "outside.txt")
    (root / ".env").write_text("beta\n")
    files = ["../outside.txt", str(tmp_path / "outside.txt"), "link.txt", ".env", "missing"]
    lines = run_worker(root, files, "beta")
    assert matches(lines) == []
    assert done(lines)["files_scanned"] == 0


def test_worker_cuts_lines(root: Path) -> None:
    (root / "long.txt").write_text("beta" + "x" * 3000 + "\n" + "y" * 2100 + "beta\n")
    lines = run_worker(root, ["long.txt"], "beta")
    found = matches(lines)
    assert [m[1] for m in found] == [1]
    assert len(found[0][2]) <= 300


def test_worker_decodes_with_replace(root: Path) -> None:
    (root / "mixed.txt").write_bytes(b"ok\n" * 3000 + b"beta \xff\n")
    lines = run_worker(root, ["mixed.txt"], "beta")
    assert matches(lines) == [("mixed.txt", 3001, "beta �")]


def test_worker_line_numbers_split_on_newline_only(root: Path) -> None:
    (root / "ff.txt").write_bytes(b"one\n\x0c\ntwo\x0cthree\na\rb\nneedle\r\n")
    lines = run_worker(root, ["ff.txt"], "needle")
    assert matches(lines) == [("ff.txt", 5, "needle")]


def test_worker_bad_regex_reports_error(root: Path) -> None:
    lines = run_worker(root, ["a.txt"], "a{99999999999999999999}", regex=True)
    assert matches(lines) == []
    final = done(lines)
    assert final["files_scanned"] == 0 and "error" in final


def test_open_scan_refuses_fifo_symlink_and_oversize(tmp_path: Path) -> None:
    import os

    from copperchat.repo_context import grep_worker

    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    assert grep_worker._open_scan(fifo) is None  # does not block on a FIFO
    target = tmp_path / "t.txt"
    target.write_text("hello\n")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    assert grep_worker._open_scan(link) is None
    big = tmp_path / "big.txt"
    big.write_bytes(b"a" * (grep_worker.FILE_MAX_BYTES + 1))
    assert grep_worker._open_scan(big) is None
    binary = tmp_path / "bin.dat"
    binary.write_bytes(b"\x00\x01")
    assert grep_worker._open_scan(binary) is None
    fh = grep_worker._open_scan(target)
    assert fh is not None
    with fh:
        assert fh.read() == b"hello\n"
