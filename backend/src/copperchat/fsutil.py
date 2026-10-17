"""Atomic file replacement that never follows a pre-planted temp-file symlink."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to a fresh, randomly named file next to ``path`` (``mkstemp``:
    O_CREAT|O_EXCL, never an existing entry) and rename it over ``path``. ``rename``
    replaces a symlink at ``path`` itself instead of writing through it."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))
