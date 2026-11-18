"""Content-addressed library blobs (spec §10 "Snapshot size", index author call 2)."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from copperchat.history.blobs import BlobStore, blob_lock
from copperchat.history.store import CorruptSnapshot
from tests.support.history import ECC83

# KiCad-saved footprints, copied (never hand-written) into the test library.
_FOOTPRINTS = sorted((ECC83 / "footprints.pretty").glob("*.kicad_mod"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def lib(tmp_path: Path) -> Path:
    d = tmp_path / "lib"
    d.mkdir()
    shutil.copyfile(_FOOTPRINTS[0], d / "A.kicad_mod")
    shutil.copyfile(_FOOTPRINTS[1], d / "B.kicad_mod")
    return d


@pytest.fixture
def blobs(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "agent")


def test_put_stores_under_content_hash(blobs: BlobStore, lib: Path) -> None:
    src = lib / "A.kicad_mod"
    sha, size = blobs.put(src, None)
    assert sha == _sha(src)
    assert size == src.stat().st_size
    assert blobs.path(sha) == blobs.root / sha
    assert blobs.root == blobs.agent_dir / "history-blobs"
    assert blobs.path(sha).read_bytes() == src.read_bytes()
    assert not list(blobs.root.glob(".tmp-*"))


def test_put_dedups_same_content(blobs: BlobStore, lib: Path) -> None:
    first = blobs.put(lib / "A.kicad_mod", None)
    shutil.copyfile(lib / "A.kicad_mod", lib / "C.kicad_mod")
    assert blobs.put(lib / "C.kicad_mod", None) == first
    assert sorted(p.name for p in blobs.root.iterdir()) == [first[0]]


def test_put_reuses_known_sha_without_reading(blobs: BlobStore, lib: Path) -> None:
    sha, size = blobs.put(lib / "A.kicad_mod", None)
    (lib / "A.kicad_mod").unlink()  # reading the source would fail now
    assert blobs.put(lib / "A.kicad_mod", sha) == (sha, size)


def test_put_with_unknown_known_sha_hashes(blobs: BlobStore, lib: Path) -> None:
    # A hint without a blob is ignored: the blob is named after the real content.
    stale = "0" * 64
    sha, _ = blobs.put(lib / "A.kicad_mod", stale)
    assert sha == _sha(lib / "A.kicad_mod")
    assert not (blobs.root / stale).exists()


def test_put_existing_blob_never_copies(
    blobs: BlobStore, lib: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Content already stored: hashed first, no temp file, no copy, no fsync."""
    from copperchat.history import blobs as blobs_mod

    sha, size = blobs.put(lib / "A.kicad_mod", None)

    def no_fsync(fd: int) -> None:
        raise AssertionError("an existing blob is never copied again")

    monkeypatch.setattr(blobs_mod.os, "fsync", no_fsync)
    monkeypatch.setattr(blobs_mod.uuid, "uuid4", lambda: pytest.fail("no temp file"))
    assert blobs.put(lib / "A.kicad_mod", None) == (sha, size)
    assert sorted(p.name for p in blobs.root.iterdir()) == [sha]


def test_put_names_blob_after_copied_bytes(
    blobs: BlobStore, lib: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source changed between the pre-hash and the copy: the copy's hash wins."""
    from copperchat.history import blobs as blobs_mod

    src = lib / "A.kicad_mod"
    real = blobs_mod._sha256_stream

    def then_change(fh: Any) -> str:
        digest = real(fh)
        shutil.copyfile(lib / "B.kicad_mod", src)  # a concurrent in-place KiCad save
        return digest

    monkeypatch.setattr(blobs_mod, "_sha256_stream", then_change)
    sha, _ = blobs.put(src, None)
    assert sha == _sha(lib / "B.kicad_mod")
    assert blobs.path(sha).read_bytes() == (lib / "B.kicad_mod").read_bytes()


def test_put_refuses_symlink_source(blobs: BlobStore, lib: Path) -> None:
    """The source is opened once without following a symlink (no check-then-open race)."""
    link = lib / "L.kicad_mod"
    link.symlink_to(lib / "A.kicad_mod")
    with pytest.raises(OSError):
        blobs.put(link, None)
    assert not blobs.root.exists() or not any(blobs.root.iterdir())


def test_put_refuses_non_regular_source(blobs: BlobStore, lib: Path) -> None:
    with pytest.raises(OSError):
        blobs.put(lib, None)  # a directory


@pytest.mark.parametrize(
    "bad", ["../../x", "", "A" * 64, "0" * 63, "0" * 65, "0" * 64 + "\n", ".tmp-x", "/etc/passwd"]
)
def test_forged_sha_refused(blobs: BlobStore, bad: str) -> None:
    with pytest.raises(CorruptSnapshot):
        blobs.path(bad)


def test_gc_removes_unreferenced(blobs: BlobStore, lib: Path) -> None:
    a, _ = blobs.put(lib / "A.kicad_mod", None)
    b, _ = blobs.put(lib / "B.kicad_mod", None)
    assert blobs.gc({a}) == 1
    assert sorted(p.name for p in blobs.root.iterdir()) == [a]
    assert blobs.gc({a}) == 0
    assert blobs.gc(set()) == 1
    assert not (blobs.root / b).exists()


def test_tmp_files_removed(blobs: BlobStore, lib: Path) -> None:
    a, _ = blobs.put(lib / "A.kicad_mod", None)
    (blobs.root / ".tmp-x").write_bytes(b"partial")
    assert blobs.gc({a}) == 1
    assert sorted(p.name for p in blobs.root.iterdir()) == [a]


def test_gc_without_blob_dir(blobs: BlobStore) -> None:
    assert blobs.gc(set()) == 0


def test_gc_never_follows_symlinked_blob_dir(tmp_path: Path, lib: Path) -> None:
    # A cloned repo could ship `.kicad-agent/history-blobs` as a link elsewhere.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    victim = elsewhere / ("0" * 64)
    shutil.copyfile(lib / "A.kicad_mod", victim)
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "history-blobs").symlink_to(elsewhere)
    blobs = BlobStore(agent)
    assert blobs.gc(set()) == 0
    assert not blobs.has_temp_files()
    assert victim.is_file()
    with pytest.raises(CorruptSnapshot):
        blobs.put(lib / "A.kicad_mod", None)


def test_gc_only_removes_own_names(blobs: BlobStore, lib: Path) -> None:
    a, _ = blobs.put(lib / "A.kicad_mod", None)
    shutil.copyfile(lib / "B.kicad_mod", blobs.root / "notes.txt")
    assert blobs.gc(set()) == 1
    assert sorted(p.name for p in blobs.root.iterdir()) == ["notes.txt"]
    assert a


def test_gc_does_not_parse_manifests(
    blobs: BlobStore, lib: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = blobs.put(lib / "A.kicad_mod", None)
    b, _ = blobs.put(lib / "B.kicad_mod", None)

    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("GC must not parse manifests")

    monkeypatch.setattr(json, "loads", boom)
    monkeypatch.setattr(json, "load", boom)
    assert blobs.gc({b}) == 1
    assert sorted(p.name for p in blobs.root.iterdir()) == [b]
    assert a != b


def test_blob_lock_per_project() -> None:
    assert blob_lock("p1") is blob_lock("p1")
    assert blob_lock("p1") is not blob_lock("p2")
