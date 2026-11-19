"""`scan_other_projects` (F4b Task 2, spec §7.8): the read-only byte search of the
repo's other projects for a lib id. The scanned files here are opaque test bytes (the
scan never parses, so they need not be KiCad documents); the `.kicad_pro` files only
mark project directories and are empty."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from copperchat.kicad_tools.footprints import usage
from copperchat.kicad_tools.footprints.usage import ScanResult, scan_other_projects

LIB_ID = "X:L1"
HIT = b'opaque bytes ("X:L1") more bytes\n'
MISS = b"opaque bytes without the id\n"


def project(root: Path, rel: str, docs: dict[str, bytes] | None = None) -> Path:
    """A project dir `rel` with an empty `<name>.kicad_pro` and the given files."""
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{d.name}.kicad_pro").write_bytes(b"")
    for name, data in (docs or {}).items():
        (d / name).write_bytes(data)
    return d


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def test_sibling_schematic_found(repo: Path) -> None:
    own = project(repo, "hardware/main", {"main.kicad_sch": MISS})
    project(repo, "hardware/io", {"io.kicad_sch": HIT, "io.kicad_pcb": MISS})
    assert scan_other_projects(repo, own, LIB_ID) == ScanResult(
        hits=["hardware/io/io.kicad_sch"], complete=True
    )


def test_board_file_found(repo: Path) -> None:
    own = project(repo, "hardware/main")
    project(repo, "hardware/io", {"io.kicad_pcb": HIT})
    assert scan_other_projects(repo, own, LIB_ID).hits == ["hardware/io/io.kicad_pcb"]


def test_own_project_skipped(repo: Path) -> None:
    own = project(repo, "hardware/main", {"main.kicad_sch": HIT, "sub.kicad_sch": HIT})
    (own / "sheets").mkdir()
    (own / "sheets" / "power.kicad_sch").write_bytes(HIT)
    assert scan_other_projects(repo, own, LIB_ID) == ScanResult(hits=[], complete=True)


def test_other_extensions_ignored(repo: Path) -> None:
    own = project(repo, "hardware/main")
    project(repo, "hardware/io", {"notes.txt": HIT, "io.kicad_pro.bak": HIT})
    assert scan_other_projects(repo, own, LIB_ID).hits == []


def test_symlinked_dir_not_followed(repo: Path, tmp_path: Path) -> None:
    own = project(repo, "hardware/main")
    outside = project(tmp_path, "outside", {"outside.kicad_sch": HIT})
    (repo / "linked").symlink_to(outside, target_is_directory=True)
    assert scan_other_projects(repo, own, LIB_ID) == ScanResult(hits=[], complete=True)


def test_symlinked_file_not_followed(repo: Path, tmp_path: Path) -> None:
    own = project(repo, "hardware/main")
    io = project(repo, "hardware/io")
    (tmp_path / "target.kicad_sch").write_bytes(HIT)
    (io / "io.kicad_sch").symlink_to(tmp_path / "target.kicad_sch")
    assert scan_other_projects(repo, own, LIB_ID).hits == []


def test_hidden_and_skip_dirs_pruned(repo: Path) -> None:
    own = project(repo, "hardware/main")
    project(repo, ".git/stash", {"a.kicad_sch": HIT})
    project(repo, "hardware/io/.kicad-agent/history", {"b.kicad_sch": HIT})
    project(repo, "node_modules/x", {"c.kicad_sch": HIT})
    project(repo, "hardware/io/__pycache__", {"d.kicad_sch": HIT})
    assert scan_other_projects(repo, own, LIB_ID).hits == []


def test_more_than_500_files_incomplete(repo: Path) -> None:
    own = project(repo, "hardware/main")
    io = project(repo, "hardware/io")
    for i in range(usage.MAX_FILES + 1):
        (io / f"s{i:04}.kicad_sch").write_bytes(MISS)
    result = scan_other_projects(repo, own, LIB_ID)
    assert result == ScanResult(hits=[], complete=False)


def test_500_files_complete(repo: Path) -> None:
    own = project(repo, "hardware/main")
    io = project(repo, "hardware/io")
    for i in range(usage.MAX_FILES):
        (io / f"s{i:04}.kicad_sch").write_bytes(MISS)
    assert scan_other_projects(repo, own, LIB_ID).complete is True


def test_file_over_20mb_skipped(repo: Path) -> None:
    own = project(repo, "hardware/main")
    io = project(repo, "hardware/io")
    with (io / "big.kicad_pcb").open("wb") as fh:  # sparse: 21 MB, the id at the end
        fh.seek(21 * 1024 * 1024)
        fh.write(HIT)
    (io / "io.kicad_sch").write_bytes(MISS)
    assert scan_other_projects(repo, own, LIB_ID) == ScanResult(hits=[], complete=False)


def test_needle_is_quoted(repo: Path) -> None:
    own = project(repo, "hardware/main")
    project(repo, "hardware/io", {"io.kicad_sch": b'(x "X:L10") (y "AX:L1")'})
    assert scan_other_projects(repo, own, LIB_ID).hits == []
    assert scan_other_projects(repo, own, "X:L10").hits == ["hardware/io/io.kicad_sch"]


def test_fifo_skipped_without_blocking(repo: Path) -> None:
    own = project(repo, "hardware/main")
    io = project(repo, "hardware/io", {"io.kicad_sch": HIT})
    os.mkfifo(io / "x.kicad_sch")
    out: list[ScanResult] = []
    worker = threading.Thread(target=lambda: out.append(scan_other_projects(repo, own, LIB_ID)))
    worker.daemon = True
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), "the scan blocked on a FIFO"
    assert out == [ScanResult(hits=["hardware/io/io.kicad_sch"], complete=True)]


def test_root_project_finds_nested_sibling(repo: Path) -> None:
    """A project at the repo root: only its own documents are skipped, not its subtree,
    so a nested sibling project is still searched."""
    (repo / "top.kicad_pro").write_bytes(b"")
    (repo / "top.kicad_sch").write_bytes(HIT)
    project(repo, "modules/io", {"io.kicad_sch": HIT})
    assert scan_other_projects(repo, repo, LIB_ID).hits == ["modules/io/io.kicad_sch"]


def test_non_project_dirs_are_searched(repo: Path) -> None:
    own = project(repo, "hardware/main")
    (repo / "archive").mkdir()
    (repo / "archive" / "old.kicad_sch").write_bytes(HIT)
    assert scan_other_projects(repo, own, LIB_ID).hits == ["archive/old.kicad_sch"]


def test_total_bytes_cap(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage, "MAX_TOTAL_BYTES", 1024 * 1024)
    own = project(repo, "hardware/main")
    io = project(repo, "hardware/io")
    (io / "a.kicad_sch").write_bytes(b"x" * (700 * 1024))
    (io / "b.kicad_sch").write_bytes(b"y" * (700 * 1024) + HIT)
    result = scan_other_projects(repo, own, LIB_ID)
    assert result == ScanResult(hits=[], complete=False)


def test_directory_cap(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage, "MAX_DIRS", 5)
    own = project(repo, "hardware/main")
    for i in range(10):
        (repo / f"d{i}").mkdir()
    assert scan_other_projects(repo, own, LIB_ID).complete is False


def test_match_across_chunk_boundary(repo: Path) -> None:
    own = project(repo, "hardware/main")
    needle = b'"X:L1"'
    pad = b"z" * (usage.CHUNK_BYTES - 3)  # the needle starts 3 bytes before the boundary
    project(repo, "hardware/io", {"io.kicad_sch": pad + needle + b"tail"})
    assert scan_other_projects(repo, own, LIB_ID).hits == ["hardware/io/io.kicad_sch"]


def test_hits_sorted_and_once_per_file(repo: Path) -> None:
    own = project(repo, "hardware/main")
    project(repo, "b/io", {"io.kicad_sch": HIT * 3})
    project(repo, "a/io", {"io.kicad_sch": HIT, "io.kicad_pcb": HIT})
    assert scan_other_projects(repo, own, LIB_ID).hits == [
        "a/io/io.kicad_pcb",
        "a/io/io.kicad_sch",
        "b/io/io.kicad_sch",
    ]
