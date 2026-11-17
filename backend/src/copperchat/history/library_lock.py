"""Process-wide lock per local footprint library directory (FP-C6).

Lock order: edit_lock -> library lock -> _apply_lock -> ...; for restore and step
checkpoints HistoryService.lock comes first. Not re-entrant; taken once at the
outermost level.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

_LOCKS: dict[Path, asyncio.Lock] = {}


def library_lock(library_dir: Path) -> asyncio.Lock:
    """The one lock for this library dir (keyed by its resolved path)."""
    return _LOCKS.setdefault(library_dir.resolve(), asyncio.Lock())


def assert_library_locked(library_dir: Path) -> None:
    if not library_lock(library_dir).locked():
        raise AssertionError(f"library lock not held for {library_dir}")
