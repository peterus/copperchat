"""The killable scan process behind repo_grep (spec §5.2).

``python -P -m copperchat.repo_context.grep_worker`` reads one JSON request
``{root, files, query, regex, case_sensitive, max_results, max_bytes}`` from stdin
and writes JSON lines to stdout (flushed one by one):

- ``{"path", "line", "text"}`` per match,
- ``{"scanned": n}`` after each scanned file (progress for a killed run),
- a final ``{"done": true, "files_scanned": n, "stopped_by": null|"max_results"|"bytes"}``.

Python's ``re`` cannot be interrupted, so the parent enforces the wall-clock budget by
killing this process. The parent has already filtered the file list; the worker still
re-checks every path (inside the root, not blocked, not KiCad, regular text file).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from copperchat.repo_context.paths import blocked_reason, is_kicad_file, is_text_file

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
        if not resolved.is_file() or resolved.stat().st_size > FILE_MAX_BYTES:
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    if blocked_reason(resolved_rel) is not None or is_kicad_file(resolved_rel):
        return None
    return resolved if is_text_file(resolved) else None


def run(request: dict[str, Any]) -> None:
    root = Path(request["root"]).resolve(strict=True)
    query: str = request["query"]
    pattern = query if request.get("regex") else re.escape(query)
    flags = 0 if request.get("case_sensitive") else re.IGNORECASE
    compiled = re.compile(pattern, flags)
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
        if path is None:
            continue
        with path.open("rb") as fh:
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
