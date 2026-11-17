"""Content-addressed library blobs (spec §10 "Snapshot size", index author call 2).

Library files are stored once per content as `.kicad-agent/history-blobs/<sha256>`,
outside `history/` (whose non-snapshot entries `_sweep_orphans` deletes). The bytes are
copies of files KiCad wrote; CopperChat never parses or edits them. A blob is written
(temp file + rename) before the manifest that references it. Blob writes and GC run
under the project's `blob_lock`, so every `.tmp-*` file GC sees is a crash leftover.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from pathlib import Path

from copperchat.errors import CopperChatError

BLOB_DIR = "history-blobs"
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_TMP_PREFIX = ".tmp-"


class CorruptSnapshot(CopperChatError):
    """The snapshot on disk does not match its manifest (edited, truncated, escaping)."""

    code = "corrupt_snapshot"
    http_status = 422


_LOCKS: dict[str, asyncio.Lock] = {}


def blob_lock(project_id: str) -> asyncio.Lock:
    """Per project: serialises blob writes (until their refs are committed) and GC.
    Taken inside the library lock, never the other way round."""
    return _LOCKS.setdefault(project_id, asyncio.Lock())


class BlobStore:
    def __init__(self, agent_dir: Path) -> None:
        self.agent_dir = agent_dir
        self.root = agent_dir / BLOB_DIR

    def path(self, sha: object) -> Path:
        """The blob's path; a sha that is not 64 lowercase hex digits is refused."""
        if not isinstance(sha, str) or _SHA_RE.fullmatch(sha) is None:
            raise CorruptSnapshot(f"invalid blob sha256: {sha!r}")
        return self.root / sha

    def put(self, src: Path, known_sha: str | None) -> tuple[str, int]:
        """Store `src` (blocking); returns (sha256, size). A `known_sha` whose blob
        exists is reused without reading `src`; otherwise the file is hashed while it is
        copied, so the blob name always equals the hash of the stored bytes."""
        if known_sha is not None:
            existing = self.path(known_sha)
            try:
                return known_sha, existing.stat().st_size
            except FileNotFoundError:
                pass
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / f"{_TMP_PREFIX}{uuid.uuid4().hex}"
        h = hashlib.sha256()
        size = 0
        try:
            with open(src, "rb") as fin, open(tmp, "xb") as fout:
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
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return sha, size

    def has_temp_files(self) -> bool:
        if not self.root.is_dir():
            return False
        return any(p.name.startswith(_TMP_PREFIX) for p in self.root.iterdir())

    def gc(self, referenced: set[str]) -> int:
        """Remove every blob not in `referenced` and every `.tmp-*` file (blocking).
        Never parses a manifest: the caller passes the refs from the database."""
        if not self.root.is_dir():
            return 0
        removed = 0
        for child in self.root.iterdir():
            name = child.name
            if not name.startswith(_TMP_PREFIX) and name in referenced:
                continue
            if child.is_dir() and not child.is_symlink():
                continue
            try:
                child.unlink()
            except FileNotFoundError:
                continue
            removed += 1
        return removed
