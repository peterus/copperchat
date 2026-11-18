"""Content-addressed library blobs (spec §10 "Snapshot size", index author call 2).

Library files are stored once per content as `.kicad-agent/history-blobs/<sha256>`,
outside `history/` (whose non-snapshot entries `_sweep_orphans` deletes). The bytes are
copies of files KiCad wrote; CopperChat never parses or edits them. A blob is written
(temp file + rename) before the manifest that references it. Blob writes and GC run
under the project's `blob_lock`, so every `.tmp-*` file GC sees is a crash leftover.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import re
import stat
import uuid
from pathlib import Path
from typing import BinaryIO

from copperchat.errors import CopperChatError

BLOB_DIR = "history-blobs"
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_TMP_PREFIX = ".tmp-"


class CorruptSnapshot(CopperChatError):
    """The snapshot on disk does not match its manifest (edited, truncated, escaping)."""

    code = "corrupt_snapshot"
    http_status = 422


def _sha256_stream(fh: BinaryIO) -> str:
    h = hashlib.sha256()
    for chunk in iter(lambda: fh.read(1 << 16), b""):
        h.update(chunk)
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as fh:
        return _sha256_stream(fh)


def _open_regular(src: Path) -> BinaryIO:
    """`src` opened ONCE for reading, never through a symlink (O_NOFOLLOW: ELOOP) and
    only if it is a regular file (fstat of the opened fd; O_NONBLOCK so a FIFO cannot
    hang the open, O_NOCTTY so a terminal never becomes the controlling one). OSError
    otherwise - no check-then-open race. Only the final component is opened without
    following; the dir components are lstat-checked by `store.tracked_files` (a swap in
    between needs same-user write access to the repo)."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(src, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(src))
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


_LOCKS: dict[str, asyncio.Lock] = {}


def blob_lock(project_id: str) -> asyncio.Lock:
    """Per project: serialises blob writes (until their refs are committed) and GC.
    Taken inside the library lock, never the other way round."""
    return _LOCKS.setdefault(project_id, asyncio.Lock())


class BlobStore:
    def __init__(self, agent_dir: Path) -> None:
        self.agent_dir = agent_dir
        self.root = agent_dir / BLOB_DIR
        self._dirty = False  # a blob was renamed into `root` since the last sync_dir

    def _root_is_real(self) -> bool:
        """The agent dir and the blob dir are real directories, never symlinks: a
        cloned repo could ship `.kicad-agent/history-blobs` as a link elsewhere, and GC
        must never delete outside CopperChat's own directory. Missing → False."""
        try:
            return all(stat.S_ISDIR(p.lstat().st_mode) for p in (self.agent_dir, self.root))
        except FileNotFoundError:
            return False

    def path(self, sha: object) -> Path:
        """The blob's path; a sha that is not 64 lowercase hex digits is refused."""
        if not isinstance(sha, str) or _SHA_RE.fullmatch(sha) is None:
            raise CorruptSnapshot(f"invalid blob sha256: {sha!r}")
        return self.root / sha

    def put(self, src: Path, known_sha: str | None) -> tuple[str, int]:
        """Store `src` (blocking); returns (sha256, size). A `known_sha` whose blob
        exists is reused without reading `src`. Otherwise `src` is hashed first and an
        existing blob of that content is reused without copying; only new content is
        copied (hashed again while copying, so the blob name always equals the hash of
        the stored bytes, also when `src` changed in between). `src` is opened once,
        without following a symlink, and must be a regular file (else OSError); the
        hash and the copy both read that one fd."""
        if known_sha is not None and self.path(known_sha).exists():
            try:
                return known_sha, self.path(known_sha).stat().st_size
            except FileNotFoundError:
                pass
        with _open_regular(src) as fin:  # one fd: hashed, then copied from
            sha = _sha256_stream(fin)
            try:
                return sha, self.path(sha).stat().st_size
            except FileNotFoundError:
                pass
            self.root.mkdir(parents=True, exist_ok=True)
            if not self._root_is_real():
                raise CorruptSnapshot(f"{self.root} is not a real directory (symlink?)")
            fin.seek(0)
            tmp = self.root / f"{_TMP_PREFIX}{uuid.uuid4().hex}"
            h = hashlib.sha256()
            size = 0
            try:
                with open(tmp, "xb") as fout:
                    for chunk in iter(lambda: fin.read(1 << 16), b""):
                        h.update(chunk)
                        fout.write(chunk)
                        size += len(chunk)
                    fout.flush()
                    os.fsync(fout.fileno())
                sha = h.hexdigest()
                target = self.path(sha)
                if target.exists():
                    tmp.unlink()
                else:
                    tmp.rename(target)
                    self._dirty = True
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        return sha, size

    def sync_dir(self) -> None:
        """fsync the blob dir if a blob was renamed into it since the last call
        (blocking), so the new directory entries survive a power loss before the refs
        naming them are committed. Called once per snapshot, after its puts."""
        if not self._dirty:
            return
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self._dirty = False

    def has_temp_files(self) -> bool:
        if not self._root_is_real():
            return False
        return any(p.name.startswith(_TMP_PREFIX) for p in self.root.iterdir())

    def gc(self, referenced: set[str]) -> int:
        """Remove every blob not in `referenced` and every `.tmp-*` file (blocking).
        Never parses a manifest: the caller passes the refs from the database."""
        if not self._root_is_real():
            return 0
        removed = 0
        for child in self.root.iterdir():
            name = child.name
            if name.startswith(_TMP_PREFIX):
                pass
            elif _SHA_RE.fullmatch(name) is None or name in referenced:
                continue  # only CopperChat's own blob names are ever removed
            try:
                if stat.S_ISDIR(child.lstat().st_mode):
                    continue
                child.unlink()  # a symlink is removed itself, never its target
            except FileNotFoundError:
                continue
            removed += 1
        return removed
