"""Tool provider ``copperchat.repo_context.specs``: repo_list, repo_grep, repo_read (spec §5).

Read-only, KiCad-less tools over the text files of the project's repository. Every
agent path is re-checked here via ``resolve_file`` / ``resolve_dir`` (the guard is the
second layer, not the only one); file I/O runs off the event loop and grep runs in a
separate worker process that is killed when its wall-clock budget expires.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from copperchat.repo_context.listing import RepoFile, list_repo
from copperchat.repo_context.paths import (
    RepoPathError,
    RepoPolicy,
    check_rel_path,
    is_kicad_file,
    is_text_file,
    repo_policy,
    resolve_dir,
    resolve_file,
)
from copperchat.tools import StructuredToolError, ToolContext, ToolResult, ToolSpec

READ_MAX_LINES = 1000
READ_MAX_BYTES = 64 * 1024
FILE_MAX_BYTES = 1024 * 1024
GREP_MAX_RESULTS = 100
GREP_DEFAULT_RESULTS = 50
GREP_BYTES = 8 * 1024 * 1024
GREP_TIMEOUT_S = 5.0
GREP_QUERY_MAX = 200
GREP_TEXT_MAX = 300
LIST_MAX = 500
LIST_DEFAULT = 200
LIST_SNIFF_MAX = 500  # entries beyond this many get kind "unknown" (no content sniff)

_WORKER_MODULE = "copperchat.repo_context.grep_worker"
_WORKER_LINE_LIMIT = 256 * 1024  # StreamReader limit: one worker line can never exceed it

_KICAD_HINT = (
    "KiCad files cannot be read as text here; use the sch_* / pcb_* / lib_* tools instead."
)
_BINARY_HINT = "binary files cannot be read; this one is listed so you know it exists."


def _invalid(message: str, hint: str) -> StructuredToolError:
    return StructuredToolError(message, "invalid input", hint)


def _path_error(exc: RepoPathError) -> StructuredToolError:
    return StructuredToolError(exc.message, "the path was refused", exc.hint)


def _str_arg(args: dict[str, Any], key: str, default: str | None) -> str | None:
    value = args.get(key, default)
    if value is None or isinstance(value, str):
        return value
    raise _invalid(f"{key} must be a string", f"Pass {key} as a string.")


def _int_arg(args: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = args.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _invalid(f"{key} must be an integer from {low} to {high}", f"Use {key}={default}.")
    return value


def _bool_arg(args: dict[str, Any], key: str) -> bool:
    value = args.get(key, False)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise _invalid(f"{key} must be true or false", f"Pass {key} as a boolean.")
    return value


async def _policy(ctx: ToolContext) -> RepoPolicy:
    return await repo_policy(ctx.project, ctx.settings.projects_root)


def _base(policy: RepoPolicy) -> dict[str, Any]:
    return {"root": policy.rel_root, "project_dir": policy.rel_project_dir}


async def _dir(policy: RepoPolicy, raw: str) -> PurePosixPath:
    try:
        _, rel = await asyncio.to_thread(resolve_dir, policy, raw)
    except RepoPathError as exc:
        raise _path_error(exc) from exc
    return rel


# --- glob matching ---------------------------------------------------------------------------


GLOB_MAX_STARS = 8


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a POSIX glob: ``*``/``?``/``[...]`` stay within one segment, ``**/`` spans
    zero or more directories and a trailing or lone ``**`` matches everything below.

    Matching is case-insensitive (file extensions differ in case across tools).
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if pattern.startswith("**", i):
            at_start = i == 0 or pattern[i - 1] == "/"
            if at_start and pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if at_start and i + 2 == n:
                out.append(".*")
                i += 2
                continue
            if not out or out[-1] != "[^/]*":  # "**" inside a segment behaves like "*"
                out.append("[^/]*")
            i += 2
            continue
        if c == "*":
            if not out or out[-1] != "[^/]*":  # "a**b"/"***": one star, no extra backtracking
                out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            end = pattern.find("]", i + 2 if pattern[i + 1 : i + 2] in ("!", "]") else i + 1)
            if end == -1:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1 : end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("(?!/)[" + body.replace("\\", "\\\\") + "]")
                i = end + 1
                continue
        else:
            out.append(re.escape(c))
        i += 1
    try:
        return re.compile("".join(out), re.IGNORECASE | re.DOTALL)
    except re.error as exc:
        raise ValueError(f"invalid glob: {exc}") from exc


def _check_glob(raw: str, key: str) -> re.Pattern[str]:
    try:
        rel = check_rel_path(raw)
    except RepoPathError as exc:
        raise _path_error(exc) from exc
    if rel == PurePosixPath("."):
        raise _invalid(f"{key} is empty", "Pass a glob such as **/*.scad.")
    # Globs are matched in-process against every listed path: bound the backtracking a
    # many-star pattern could cause (each star is a nested [^/]* loop).
    if rel.as_posix().count("*") > GLOB_MAX_STARS:
        raise _invalid(
            f"{key} has too many wildcards", f"Use at most {GLOB_MAX_STARS} '*' characters."
        )
    try:
        return glob_to_regex(rel.as_posix())
    except ValueError as exc:
        raise _invalid(f"{key} is not a valid glob", "Check the [...] character classes.") from exc


def _under(files: Iterable[RepoFile], base: PurePosixPath) -> list[tuple[RepoFile, str]]:
    """Files below `base` with their path relative to `base`."""
    if base == PurePosixPath("."):
        return [(f, f.rel.as_posix()) for f in files]
    depth = len(base.parts)
    return [
        (f, PurePosixPath(*f.rel.parts[depth:]).as_posix())
        for f in files
        if len(f.rel.parts) > depth and f.rel.parts[:depth] == base.parts
    ]


# --- repo_list -------------------------------------------------------------------------------


def _kinds(root: Path, files: list[RepoFile]) -> list[str]:
    kinds: list[str] = []
    for index, f in enumerate(files):
        if is_kicad_file(f.rel):
            kinds.append("kicad")
        elif index >= LIST_SNIFF_MAX:
            kinds.append("unknown")
        elif not is_text_file(root / f.rel):
            kinds.append("binary")
        elif f.size > FILE_MAX_BYTES:
            kinds.append("large")
        else:
            kinds.append("text")
    return kinds


async def _repo_list(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    raw_path = _str_arg(args, "path", ".") or "."
    raw_pattern = _str_arg(args, "pattern", None)
    max_entries = _int_arg(args, "max_entries", LIST_DEFAULT, 1, LIST_MAX)
    matcher = _check_glob(raw_pattern, "pattern") if raw_pattern else None
    policy = await _policy(ctx)
    base = await _dir(policy, raw_path)
    listing = await list_repo(policy)
    below = _under(listing.files, base)

    dirs: dict[str, int] = {}
    if matcher is not None:
        selected = [f for f, sub in below if matcher.fullmatch(sub)]
    else:
        selected = []
        for f, sub in below:
            head, sep, _ = sub.partition("/")
            if sep:
                dirs[head] = dirs.get(head, 0) + 1
            else:
                selected.append(f)

    shown = selected[:max_entries]
    kinds = await asyncio.to_thread(_kinds, policy.root, shown)
    data = _base(policy) | {
        "path": base.as_posix(),
        "entries": [
            {"path": f.rel.as_posix(), "size": f.size, "kind": kind}
            for f, kind in zip(shown, kinds, strict=True)
        ],
        "total": len(selected),
        "truncated": len(selected) > max_entries or listing.truncated,
    }
    if matcher is None:
        data["dirs"] = [
            {"path": (base / name).as_posix(), "files": count}
            for name, count in sorted(dirs.items())
        ]
    return ToolResult(data=data)


# --- repo_read -------------------------------------------------------------------------------


def _read_lines(path: Path, rel: str) -> list[str]:
    """The decoded lines of a readable text file (checks size and content first)."""
    size = path.stat().st_size
    if size > FILE_MAX_BYTES:
        raise StructuredToolError(
            f"{rel} is larger than 1 MiB",
            f"the file has {size} bytes",
            "Use repo_grep to find the relevant lines instead.",
        )
    if not is_text_file(path):
        raise StructuredToolError(f"{rel} is a binary file", "not UTF-8 text", _BINARY_HINT)
    return path.read_bytes().decode("utf-8", errors="replace").splitlines(keepends=True)


def _cut_utf8(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


async def _repo_read(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    raw_path = _str_arg(args, "path", None)
    if not raw_path:
        raise _invalid("path is required", "Pass a file path relative to the repository root.")
    offset = _int_arg(args, "offset", 1, 1, 10**9)
    limit = _int_arg(args, "limit", READ_MAX_LINES, 1, READ_MAX_LINES)
    policy = await _policy(ctx)
    try:
        resolved, rel = await asyncio.to_thread(resolve_file, policy, raw_path)
    except RepoPathError as exc:
        raise _path_error(exc) from exc
    resolved_rel = PurePosixPath(resolved.relative_to(policy.root).as_posix())
    if is_kicad_file(rel) or is_kicad_file(resolved_rel):
        raise StructuredToolError(f"{rel} is a KiCad file", "not readable as text", _KICAD_HINT)
    lines = await asyncio.to_thread(_read_lines, resolved, rel.as_posix())

    total = len(lines)
    if offset > max(total, 1):
        raise StructuredToolError(
            f"offset {offset} is beyond the end of {rel}",
            f"the file has {total} lines",
            f"Pass an offset from 1 to {max(total, 1)}.",
        )
    parts: list[str] = []
    used = 0
    end = offset - 1
    for line in lines[offset - 1 : offset - 1 + limit]:
        size = len(line.encode("utf-8"))
        if used + size > READ_MAX_BYTES:
            if not parts:  # the first line alone is over the cap: cut the line
                parts.append(_cut_utf8(line, READ_MAX_BYTES))
                end += 1
            break
        parts.append(line)
        used += size
        end += 1
    truncated = end < total
    data = _base(policy) | {
        "path": rel.as_posix(),
        "content": "".join(parts),
        "start_line": offset,
        "end_line": end,
        "total_lines": total,
        "truncated": truncated,
    }
    if truncated:
        data["next_offset"] = end + 1
    return ToolResult(data=data)


# --- repo_grep -------------------------------------------------------------------------------


class _WorkerFailed(Exception):
    pass


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    await proc.wait()


async def _run_worker(
    request: dict[str, Any], max_results: int
) -> tuple[list[dict[str, Any]], int, str | None]:
    """Stream matches from the worker until done, the budget or the deadline."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + GREP_TIMEOUT_S
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-P",  # never put the cwd on sys.path
        "-m",
        _WORKER_MODULE,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=_WORKER_LINE_LIMIT,
    )
    assert proc.stdin is not None and proc.stdout is not None
    matches: list[dict[str, Any]] = []
    files_scanned = 0
    stopped_by: str | None = None
    final: dict[str, Any] | None = None
    try:
        try:
            async with asyncio.timeout_at(deadline):
                proc.stdin.write(json.dumps(request).encode())
                await proc.stdin.drain()
                proc.stdin.close()
                while final is None:
                    try:
                        line = await proc.stdout.readline()
                    except ValueError as exc:  # line over the StreamReader limit
                        raise _WorkerFailed("worker output line too long") from exc
                    if not line:
                        break
                    msg = json.loads(line)
                    if msg.get("done") is True:
                        final = msg
                    elif "scanned" in msg:
                        files_scanned = int(msg["scanned"])
                    elif len(matches) < max_results:
                        matches.append(
                            {
                                "path": str(msg["path"]),
                                "line": int(msg["line"]),
                                "text": str(msg["text"])[:GREP_TEXT_MAX],
                            }
                        )
                await proc.wait()
        except TimeoutError:
            stopped_by = "time"
        except (BrokenPipeError, ConnectionResetError, ValueError, KeyError) as exc:
            raise _WorkerFailed(str(exc)) from exc
    finally:
        await _kill(proc)
    if stopped_by == "time":
        return matches, files_scanned, stopped_by
    if final is None:
        raise _WorkerFailed(f"worker exited with code {proc.returncode} without a result")
    return matches, int(final.get("files_scanned", files_scanned)), final.get("stopped_by")


async def _repo_grep(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = _str_arg(args, "query", None)
    if not query or len(query) > GREP_QUERY_MAX:
        raise _invalid(
            f"query must be 1 to {GREP_QUERY_MAX} characters", "Pass a shorter search string."
        )
    regex = _bool_arg(args, "regex")
    case_sensitive = _bool_arg(args, "case_sensitive")
    raw_path = _str_arg(args, "path", ".") or "."
    raw_glob = _str_arg(args, "glob", None)
    max_results = _int_arg(args, "max_results", GREP_DEFAULT_RESULTS, 1, GREP_MAX_RESULTS)
    try:
        re.compile(query if regex else re.escape(query), 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        raise StructuredToolError(
            "query is not a valid regular expression",
            str(exc),
            "Fix the pattern, or pass regex=false to search for the literal text.",
        ) from exc
    matcher = _check_glob(raw_glob, "glob") if raw_glob else None
    by_name = raw_glob is not None and "/" not in raw_glob
    policy = await _policy(ctx)
    base = await _dir(policy, raw_path)
    listing = await list_repo(policy)

    files = [
        f.rel.as_posix()
        for f, sub in _under(listing.files, base)
        if not is_kicad_file(f.rel)
        and f.size <= FILE_MAX_BYTES
        and (matcher is None or matcher.fullmatch(f.rel.name if by_name else sub))
    ]
    request = {
        "root": str(policy.root),
        "files": files,
        "query": query,
        "regex": regex,
        "case_sensitive": case_sensitive,
        "max_results": max_results,
        "max_bytes": GREP_BYTES,
    }
    try:
        matches, files_scanned, stopped_by = await _run_worker(request, max_results)
    except (OSError, _WorkerFailed) as exc:
        raise StructuredToolError(
            "repo_grep failed",
            f"the grep worker did not finish: {exc}",
            "Try again with a simpler query or a narrower path.",
        ) from exc
    return ToolResult(
        data=_base(policy)
        | {
            "matches": matches,
            "files_scanned": files_scanned,
            "truncated": stopped_by is not None or listing.truncated,
            "stopped_by": stopped_by,
        }
    )


# --- specs -----------------------------------------------------------------------------------

_PATH_PROP = {"type": "string", "maxLength": 400}


def _spec(
    name: str, description: str, props: dict[str, Any], required: list[str], handler: Any
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
        handler=handler,
        mutating=False,
        changes_documents=False,
        requires_kicad=False,
    )


TOOL_SPECS = [
    _spec(
        "repo_list",
        "List files of the project's repository (read-only). Paths are relative to the "
        "repository root. Without `pattern` you get the files directly in `path` plus its "
        "sub-directories with file counts; with `pattern` (a glob relative to `path`, `**` "
        "spans directories, e.g. **/*.scad; `**` alone lists everything below) you get every "
        "matching file. Each entry has a kind: text, binary, kicad, large (over 1 MiB) or "
        "unknown. Secrets, .git and CopperChat state are never listed.",
        {
            "path": _PATH_PROP | {"description": "Directory relative to the root (default .)"},
            "pattern": _PATH_PROP | {"description": "Optional glob relative to path"},
            "max_entries": {
                "type": "integer",
                "minimum": 1,
                "maximum": LIST_MAX,
                "description": f"Default {LIST_DEFAULT}, max {LIST_MAX}",
            },
        },
        [],
        _repo_list,
    ),
    _spec(
        "repo_grep",
        "Search the text files of the project's repository (read-only) for a string "
        "(literal and case-insensitive by default, or a Python regex with regex=true). "
        "KiCad files, binaries, secrets and files over 1 MiB are skipped. Returns matching "
        "lines with path and line number; the search stops at max_results, 8 MiB scanned "
        "or 5 s and says which limit hit (stopped_by).",
        {
            "query": {"type": "string", "minLength": 1, "maxLength": GREP_QUERY_MAX},
            "regex": {"type": "boolean", "description": "Treat query as a regex (default false)"},
            "case_sensitive": {"type": "boolean", "description": "Default false"},
            "path": _PATH_PROP | {"description": "Directory to search (default .)"},
            "glob": _PATH_PROP
            | {"description": "Optional file filter, e.g. *.scad (no '/' = file name only)"},
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": GREP_MAX_RESULTS,
                "description": f"Default {GREP_DEFAULT_RESULTS}, max {GREP_MAX_RESULTS}",
            },
        },
        ["query"],
        _repo_grep,
    ),
    _spec(
        "repo_read",
        "Read a text file of the project's repository (read-only), at most 1000 lines and "
        "64 KiB per call; continue with offset=next_offset when truncated. KiCad files (use "
        "the sch_* / pcb_* / lib_* tools), binaries, secrets and files over 1 MiB are refused.",
        {
            "path": _PATH_PROP | {"description": "File path relative to the repository root"},
            "offset": {"type": "integer", "minimum": 1, "description": "1-based start line"},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": READ_MAX_LINES,
                "description": f"Lines to return (default and max {READ_MAX_LINES})",
            },
        },
        ["path"],
        _repo_read,
    ),
]
