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
import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from copperchat.gitproc import GitError, run_git
from copperchat.repo_context.listing import Listing, ListingError, RepoFile, list_repo
from copperchat.repo_context.paths import (
    RepoPathError,
    RepoPolicy,
    check_rel_path,
    is_kicad_file,
    is_text_bytes,
    is_text_file,
    open_regular,
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
IGNORE_TIMEOUT_S = 5.0
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
    """The directory `raw` as resolved, relative to the root (a symlinked directory is
    replaced by its target, since the listing holds files under their real paths)."""
    try:
        resolved, _ = await asyncio.to_thread(resolve_dir, policy, raw)
    except RepoPathError as exc:
        raise _path_error(exc) from exc
    rel = resolved.relative_to(policy.root).as_posix()
    return PurePosixPath(rel) if rel else PurePosixPath(".")


async def _listing(policy: RepoPolicy) -> Listing:
    try:
        return await list_repo(policy)
    except ListingError as exc:
        raise StructuredToolError(
            "git listing failed",
            f"git ls-files did not succeed: {exc}",
            "Try again shortly; if it keeps failing, the repository's git state needs repair "
            "(e.g. run git status in it).",
        ) from exc


# --- glob matching ---------------------------------------------------------------------------


_Token = tuple[str, Any]  # ("*", None) | ("?", None) | ("lit", ch) | ("cls", (negate, ranges))


def _parse_class(segment: str, i: int) -> tuple[_Token, int] | None:
    """The ``[...]`` class starting at ``segment[i]`` and the index after it, or None if
    the ``[`` is unterminated (then it is a literal). ``!`` negates, a leading ``]`` is a
    member, ``a-c`` is a range; a reversed range is an invalid glob (ValueError)."""
    j = i + 1
    negate = segment[j : j + 1] == "!"
    if negate:
        j += 1
    body_start = j
    if segment[j : j + 1] == "]":
        j += 1
    end = segment.find("]", j)
    if end == -1:
        return None
    body = segment[body_start:end]
    ranges: list[tuple[str, str]] = []
    k = 0
    while k < len(body):
        if k + 2 < len(body) and body[k + 1] == "-":
            low, high = body[k], body[k + 2]
            if low > high:
                raise ValueError(f"reversed range {low}-{high}")
            ranges.append((low, high))
            k += 3
        else:
            ranges.append((body[k], body[k]))
            k += 1
    return ("cls", (negate, tuple(ranges))), end + 1


def _compile_segment(segment: str) -> list[_Token]:
    tokens: list[_Token] = []
    i = 0
    while i < len(segment):
        c = segment[i]
        if c == "*":
            if not tokens or tokens[-1][0] != "*":  # "**"/"***" inside a segment = "*"
                tokens.append(("*", None))
        elif c == "?":
            tokens.append(("?", None))
        elif c == "[" and (parsed := _parse_class(segment, i)) is not None:
            tokens.append(parsed[0])
            i = parsed[1]
            continue
        else:
            tokens.append(("lit", c))
        i += 1
    return tokens


def _token_matches(token: _Token, ch: str) -> bool:
    kind, value = token
    if kind == "?":
        return True
    if kind == "lit":
        return bool(value == ch)
    negate, ranges = value
    return bool(any(low <= ch <= high for low, high in ranges) != negate)


def _segment_matches(tokens: list[_Token], name: str) -> bool:
    """Greedy two-pointer wildcard match: O(len(tokens) * len(name)) worst case."""
    t = n = 0
    star_t, star_n = -1, 0
    while n < len(name):
        if t < len(tokens) and tokens[t][0] == "*":
            star_t, star_n = t, n
            t += 1
        elif t < len(tokens) and _token_matches(tokens[t], name[n]):
            t += 1
            n += 1
        elif star_t != -1:  # let the last star absorb one more character and retry
            t = star_t + 1
            star_n += 1
            n = star_n
        else:
            return False
    while t < len(tokens) and tokens[t][0] == "*":
        t += 1
    return t == len(tokens)


class Glob:
    """A compiled POSIX glob, matched in linear time per (pattern segment, path segment).

    ``*``/``?``/``[...]`` stay within one segment (an unterminated ``[`` is literal, ``**``
    inside a segment acts like ``*``); a ``**`` segment spans zero or more directories, a
    trailing ``**`` everything below (one or more segments); consecutive ``**`` segments
    collapse into one. Matching is case-insensitive
    (file extensions differ in case across tools). There is no backtracking across
    segments: the match runs a set of reachable path positions through the pattern
    segments, so a pattern of P segments costs O(P * S) segment matches on S segments.
    """

    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        self._segments: list[list[_Token] | None] = []  # None = "**"
        for seg in pattern.lower().split("/"):
            if seg != "**":
                self._segments.append(_compile_segment(seg))
            elif not self._segments or self._segments[-1] is not None:
                self._segments.append(None)  # "**/**" spans the same as one "**"

    def matches(self, path: str) -> bool:
        names = path.lower().split("/")
        last = len(self._segments) - 1
        states = {0}
        for index, tokens in enumerate(self._segments):
            if not states:
                return False
            if tokens is None:
                first = min(states) + (1 if index == last else 0)
                states = set(range(first, len(names) + 1))
            else:
                states = {
                    j + 1 for j in states if j < len(names) and _segment_matches(tokens, names[j])
                }
        return len(names) in states


def glob_match(pattern: str, path: str) -> bool:
    """True if `path` (POSIX, relative) matches the glob `pattern` (see `Glob`)."""
    return Glob(pattern).matches(path)


def _check_glob(raw: str, key: str) -> Glob:
    try:
        rel = check_rel_path(raw)
    except RepoPathError as exc:
        raise _path_error(exc) from exc
    if rel == PurePosixPath("."):
        raise _invalid(f"{key} is empty", "Pass a glob such as **/*.scad.")
    # No wildcard cap: matching is linear per segment pair and check_rel_path bounds the
    # pattern to MAX_PATH_LEN characters.
    try:
        return Glob(rel.as_posix())
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


def _select(
    files: Iterable[RepoFile], base: PurePosixPath, matcher: Glob | None
) -> tuple[list[RepoFile], dict[str, int]]:
    """The entries of a repo_list call: every file below `base` matching `matcher`, or
    without one the files directly in `base` plus the file counts of its sub-directories."""
    below = _under(files, base)
    dirs: dict[str, int] = {}
    if matcher is not None:
        return [f for f, sub in below if matcher.matches(sub)], dirs
    selected = []
    for f, sub in below:
        head, sep, _ = sub.partition("/")
        if sep:
            dirs[head] = dirs.get(head, 0) + 1
        else:
            selected.append(f)
    return selected, dirs


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
    listing = await _listing(policy)
    selected, dirs = await asyncio.to_thread(_select, listing.files, base, matcher)

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


def _too_large(rel: str, size: str) -> StructuredToolError:
    return StructuredToolError(
        f"{rel} is larger than 1 MiB",
        f"the file has {size} bytes",
        "Use repo_grep to find the relevant lines instead.",
    )


def split_lines(text: str) -> list[str]:
    """Lines as repo_grep numbers them: split on ``\\n`` only, ends kept (a ``\\r``
    stays in the content), so a grep hit's line number is the repo_read offset."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _read_lines(path: Path, rel: str) -> list[str]:
    """The decoded lines of a readable text file: one open of the already-resolved path,
    at most 1 MiB + 1 byte read (a file growing after the size check is still refused),
    the text sniff runs on the bytes read."""
    with os.fdopen(open_regular(path), "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        if size > FILE_MAX_BYTES:
            raise _too_large(rel, str(size))
        data = fh.read(FILE_MAX_BYTES + 1)
    if len(data) > FILE_MAX_BYTES:
        raise _too_large(rel, f"more than {FILE_MAX_BYTES}")
    if not is_text_bytes(data):
        raise StructuredToolError(f"{rel} is a binary file", "not UTF-8 text", _BINARY_HINT)
    return split_lines(data.decode("utf-8", errors="replace"))


def _physical_entry(policy: RepoPolicy, rel: PurePosixPath) -> PurePosixPath:
    """`rel` with its directory resolved: the entry as it physically exists in the root.

    ``git check-ignore`` refuses paths "beyond a symbolic link", so a path read through
    an in-root directory symlink is checked at its real location; the last component
    is kept as given, so an ignored file symlink is still checked under its own name.
    """
    try:
        parent = (policy.root / rel.parent).resolve(strict=True).relative_to(policy.root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise StructuredToolError(
            f"{rel} could not be checked against .gitignore",
            "its directory does not resolve inside the repository",
            "Use repo_list to see the available files.",
        ) from exc
    return PurePosixPath(parent.as_posix()) / rel.name


class IgnoreCheckUnavailable(StructuredToolError):
    """git could not answer whether a path is ignored (transient; the read is refused)."""


async def _check_not_ignored(policy: RepoPolicy, rels: Iterable[PurePosixPath]) -> None:
    """Refuse a path git ignores (spec D8); tracked files are never "ignored" here.

    Fails closed: if git cannot answer, the read is refused too. Paths inside a git
    submodule (which check-ignore rejects) are refused as such; the listing never
    shows them either, since ``git ls-files`` does not recurse into submodules.
    """
    for rel in dict.fromkeys(rels):
        try:
            # "./" keeps git from parsing a leading ":" as magic pathspec syntax.
            code, _, err = await run_git(
                policy.root,
                "check-ignore",
                "-q",
                "--",
                f"./{rel.as_posix()}",
                timeout_s=IGNORE_TIMEOUT_S,
            )
        except GitError as exc:
            raise IgnoreCheckUnavailable(
                f"{rel} could not be checked against .gitignore",
                str(exc) or type(exc).__name__,
                "Try again in a moment.",
            ) from exc
        if code == 0:
            raise StructuredToolError(
                f"{rel} is ignored by .gitignore",
                "the path was refused",
                "Ignored files are not readable; use repo_list to see the available files.",
            )
        sub = re.search(rb"is in submodule '([^']*)'", err) if code != 1 else None
        if sub is not None:  # git_env pins LC_ALL=C
            raise StructuredToolError(
                f"{rel} is inside git submodule {os.fsdecode(sub.group(1))}",
                "submodule contents are not readable",
                "Only files of this repository itself are available; use repo_list to see them.",
            )
        if code != 1:
            raise IgnoreCheckUnavailable(
                f"{rel} could not be checked against .gitignore",
                f"git check-ignore exited with code {code}",
                "Use repo_list to see the available files.",
            )


def _cut_utf8(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


async def resolve_readable(policy: RepoPolicy, raw_path: str) -> tuple[Path, PurePosixPath]:
    """``repo_read``'s file acceptance: path policy, not a KiCad file (as given or as
    resolved), not ignored by git. Raises `StructuredToolError`. Also used by the brief."""
    try:
        resolved, rel = await asyncio.to_thread(resolve_file, policy, raw_path)
    except RepoPathError as exc:
        raise _path_error(exc) from exc
    resolved_rel = PurePosixPath(resolved.relative_to(policy.root).as_posix())
    if is_kicad_file(rel) or is_kicad_file(resolved_rel):
        raise StructuredToolError(f"{rel} is a KiCad file", "not readable as text", _KICAD_HINT)
    if policy.is_git:
        entry_rel = await asyncio.to_thread(_physical_entry, policy, rel)
        await _check_not_ignored(policy, (entry_rel, resolved_rel))
    return resolved, rel


async def _repo_read(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    raw_path = _str_arg(args, "path", None)
    if not raw_path:
        raise _invalid("path is required", "Pass a file path relative to the repository root.")
    offset = _int_arg(args, "offset", 1, 1, 10**9)
    limit = _int_arg(args, "limit", READ_MAX_LINES, 1, READ_MAX_LINES)
    policy = await _policy(ctx)
    resolved, rel = await resolve_readable(policy, raw_path)
    try:
        lines = await asyncio.to_thread(_read_lines, resolved, rel.as_posix())
    except OSError as exc:
        raise StructuredToolError(
            f"{rel} could not be read",
            exc.strerror or type(exc).__name__,
            "Use repo_list to check that the file still exists.",
        ) from exc

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
    line_cut = False
    for line in lines[offset - 1 : offset - 1 + limit]:
        size = len(line.encode("utf-8"))
        if used + size > READ_MAX_BYTES:
            if not parts:  # the first line alone is over the cap: cut the line
                parts.append(_cut_utf8(line, READ_MAX_BYTES))
                end += 1
                line_cut = True
            break
        parts.append(line)
        used += size
        end += 1
    truncated = end < total or line_cut
    data = _base(policy) | {
        "path": rel.as_posix(),
        "content": "".join(parts),
        "start_line": offset,
        "end_line": end,
        "total_lines": total,
        "truncated": truncated,
    }
    if line_cut:
        # The rest of a cut line cannot be reached by any offset; continue after it.
        data["line_truncated"] = True
    if end < total:
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


def _worker_env() -> dict[str, str]:
    """A minimal environment for the grep worker: no server secrets (API keys, tokens,
    passwords) are inherited. The venv interpreter finds its site-packages without any
    variables; PYTHONPATH is passed through only if the server itself runs with one."""
    env = {"LANG": "C", "LC_ALL": "C", "PYTHONIOENCODING": "utf-8"}
    for key in ("PATH", "PYTHONPATH"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


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
        env=_worker_env(),
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
    if final.get("error"):
        raise _WorkerFailed(f"the query was refused: {final['error']}")
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
        flags = 0 if case_sensitive else re.IGNORECASE
        await asyncio.to_thread(re.compile, query if regex else re.escape(query), flags)
    except (re.error, OverflowError) as exc:
        raise StructuredToolError(
            "query is not a valid regular expression",
            str(exc),
            "Fix the pattern, or pass regex=false to search for the literal text.",
        ) from exc
    matcher = _check_glob(raw_glob, "glob") if raw_glob else None
    by_name = matcher is not None and "/" not in matcher.pattern
    policy = await _policy(ctx)
    base = await _dir(policy, raw_path)
    listing = await _listing(policy)

    def select() -> list[str]:
        return [
            f.rel.as_posix()
            for f, sub in _under(listing.files, base)
            if not is_kicad_file(f.rel)
            and f.size <= FILE_MAX_BYTES
            and (matcher is None or matcher.matches(f.rel.name if by_name else sub))
        ]

    files = await asyncio.to_thread(select)
    request = {
        "root": str(policy.root),
        "files": files,
        "query": query,
        "regex": regex,
        "case_sensitive": case_sensitive,
        "max_results": max_results,
        "max_bytes": GREP_BYTES,
        "git_dirs": [d.as_posix() for d in policy.git_dirs],
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
        "64 KiB per call; continue with offset=next_offset when truncated (line_truncated means "
        "a single line was longer than 64 KiB and was cut). KiCad files (use "
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
