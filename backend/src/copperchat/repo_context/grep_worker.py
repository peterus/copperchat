"""The killable scan process behind repo_grep (spec §5.2).

``python -P -m copperchat.repo_context.grep_worker`` reads one JSON request
``{root, files, query, regex, case_sensitive, max_results, max_bytes}`` from stdin
and writes JSON lines to stdout (flushed one by one):

- ``{"path", "line", "text"}`` per match,
- ``{"scanned": n}`` after each scanned file (progress for a killed run),
- a final ``{"done": true, "files_scanned": n, "stopped_by": null|"max_results"|"bytes"}``
  (with an ``"error"`` and no scan if the query does not compile).

Lines are split on ``\\n`` only (as ``repo_read`` does), so line numbers agree.

Python's ``re`` cannot be interrupted, so the parent enforces the wall-clock budget by
killing this process. The parent has already filtered the file list; the worker still
re-checks every path (inside the root, not blocked, not KiCad) and opens it without
following symlinks, then checks the open file (regular, at most 1 MiB, text).
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from copperchat.repo_context.paths import SNIFF_BYTES, blocked_reason, is_kicad_file, is_text_bytes

LINE_MATCH_MAX = 2000
TEXT_OUT_MAX = 300
FILE_MAX_BYTES = 1024 * 1024  # mirrors specs.FILE_MAX_BYTES (kept import-light here)


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _safe_file(root: Path, raw: str) -> Path | None:
    rel = PurePosixPath(raw)
    if rel.is_absolute() or ".." in rel.parts or "\\" in raw or not rel.parts:
        return None
    if blocked_reason(rel) is not None or is_kicad_file(rel):
        return None
    try:
        resolved = (root / rel).resolve(strict=True)
        resolved_rel = PurePosixPath(resolved.relative_to(root).as_posix())
    except (OSError, RuntimeError, ValueError):
        return None
    if blocked_reason(resolved_rel) is not None or is_kicad_file(resolved_rel):
        return None
    return resolved


_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


def _open_scan(path: Path) -> BinaryIO | None:
    """`path` opened for scanning, or None: no symlink is followed (the path was
    resolved already), a FIFO or device does not block, and the regular-file, size
    and text checks run on the opened file, so a swap after the checks cannot slip in."""
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except OSError:
        return None
    fh = os.fdopen(fd, "rb")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > FILE_MAX_BYTES:
            fh.close()
            return None
        head = fh.read(SNIFF_BYTES)
        if not is_text_bytes(head):
            fh.close()
            return None
        fh.seek(0)
    except OSError:
        fh.close()
        return None
    return fh


def run(request: dict[str, Any]) -> None:
    root = Path(request["root"]).resolve(strict=True)
    query: str = request["query"]
    pattern = query if request.get("regex") else re.escape(query)
    flags = 0 if request.get("case_sensitive") else re.IGNORECASE
    try:
        compiled = re.compile(pattern, flags)
    except (re.error, OverflowError) as exc:
        _emit({"done": True, "files_scanned": 0, "stopped_by": None, "error": str(exc)})
        return
    max_results = int(request["max_results"])
    max_bytes = int(request["max_bytes"])

    found = 0
    scanned_bytes = 0
    files_scanned = 0
    stopped_by: str | None = None
    for raw in request["files"]:
        if not isinstance(raw, str):
            continue
        path = _safe_file(root, raw)
        fh = _open_scan(path) if path is not None else None
        if fh is None:
            continue
        with fh:
            for number, line in enumerate(fh, start=1):
                scanned_bytes += len(line)
                if scanned_bytes > max_bytes:
                    stopped_by = "bytes"
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")[:LINE_MATCH_MAX]
                if compiled.search(text) is None:
                    continue
                _emit({"path": raw, "line": number, "text": text[:TEXT_OUT_MAX]})
                found += 1
                if found >= max_results:
                    stopped_by = "max_results"
                    break
        files_scanned += 1
        _emit({"scanned": files_scanned})
        if stopped_by is not None:
            break
    _emit({"done": True, "files_scanned": files_scanned, "stopped_by": stopped_by})


def main() -> None:
    run(json.load(sys.stdin))


if __name__ == "__main__":
    main()
