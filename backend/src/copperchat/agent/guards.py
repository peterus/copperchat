"""Permission guards (``can_use_tool``) of the main agent and the part-researcher."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext
from claude_agent_sdk.types import (
    CanUseTool,
    HookCallback,
    HookContext,
    HookInput,
    HookJSONOutput,
    PermissionResult,
)

from copperchat.agent.profiles import REPO_TOOLS
from copperchat.agent.sdk_tools import sdk_tool_name
from copperchat.parts import InvalidMpn, mpn_dirname, notes_user_edited
from copperchat.projects import Project
from copperchat.repo_context.paths import (
    RepoPathError,
    RepoPolicy,
    check_rel_path,
    is_kicad_file,
    is_text_file,
    resolve_dir,
    resolve_file,
)
from copperchat.repo_context.specs import FILE_MAX_BYTES
from copperchat.research.agent import PARTS_TOOLS, RESEARCHER_TOOLS
from copperchat.research.net import Resolver, url_refusal
from copperchat.research.paths import (
    parts_root,
    resolve_inside,
    uploads_image_root,
    uploads_pdf_root,
)

# Our MCP tool that runs the part-researcher (index "Decided by owner" item 8).
DELEGATION_TOOL = "mcp__copperchat__research_part"
SKILL_TOOL = "Skill"
_PATH_KEY = {"Read": "file_path", "Write": "file_path", "Edit": "file_path", "Glob": "path"}
_WRITABLE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.(md|txt)$")

ToolCallObserver = Callable[[str, dict[str, Any], ToolPermissionContext], None]

# SDK name -> bare name of the read-only repository tools (repo-context spec §5.5).
_REPO_SDK_TOOLS = {sdk_tool_name(name): name for name in REPO_TOOLS}
_REPO_GLOB_KEY = {"repo_list": "pattern", "repo_grep": "glob"}


def _deny(msg: str) -> PermissionResult:
    return PermissionResultDeny(message=msg, interrupt=False)


def _refusal(exc: RepoPathError) -> str:
    return f"{exc.message} {exc.hint}".strip()


def _repo_read_refusal(policy: RepoPolicy, raw: object) -> str | None:
    if not isinstance(raw, str) or not raw:
        return "repo_read needs `path`: a file path relative to the repository root."
    resolved, rel = resolve_file(policy, raw)
    resolved_rel = PurePosixPath(resolved.relative_to(policy.root).as_posix())
    if is_kicad_file(rel) or is_kicad_file(resolved_rel):
        return (
            f"{rel.as_posix()} is a KiCad file and cannot be read as text; use the "
            "sch_* / pcb_* / lib_* tools instead."
        )
    if resolved.stat().st_size > FILE_MAX_BYTES:
        return f"{rel.as_posix()} is larger than 1 MiB; use repo_grep to find the relevant lines."
    if not is_text_file(resolved):
        return f"{rel.as_posix()} is a binary file; binary files cannot be read."
    return None


def repo_path_refusal(policy: RepoPolicy, tool: str, inp: dict[str, Any]) -> str | None:
    """Why the repo_* call ``tool`` (bare or SDK name) with ``inp`` is refused, or None.

    The full path policy against the cached repository root (spec §5.5): ``repo_read``
    gets the file checks incl. KiCad, size and binary sniff; ``repo_list`` / ``repo_grep``
    get the directory check on ``path`` (default ``.``) and the static checks on their
    glob (``pattern`` / ``glob``). A value of the wrong type is refused. Blocking file
    system I/O: call it off the event loop. The tool handler checks again.
    """
    name = _REPO_SDK_TOOLS.get(tool, tool)
    try:
        if name == "repo_read":
            return _repo_read_refusal(policy, inp.get("path"))
        glob_key = _REPO_GLOB_KEY.get(name)
        if glob_key is None:
            return f"{tool} is not a repository tool."
        raw_dir = inp.get("path")
        if raw_dir is not None and not isinstance(raw_dir, str):
            return "`path` must be a string."
        resolve_dir(policy, raw_dir or ".")
        pattern = inp.get(glob_key)
        if pattern is not None and not isinstance(pattern, str):
            return f"`{glob_key}` must be a string."
        if pattern:
            check_rel_path(pattern)
    except RepoPathError as exc:
        return _refusal(exc)
    except OSError as exc:
        return f"The path could not be checked ({exc.strerror or type(exc).__name__})."
    return None


def make_main_guard(
    allowed: frozenset[str],
    *,
    repo: RepoPolicy | None = None,
    on_allowed: ToolCallObserver | None = None,
) -> CanUseTool:
    """The main agent's guard: exactly the registered CopperChat MCP tools plus ``Skill``.

    ``on_allowed`` sees every allowed call before it runs; the SDK backend uses it to learn
    the ``tool_use_id`` of a ``research_part`` call (the MCP handler itself does not get it).
    The researcher's own tools are refused here with a hint to delegate. ``repo_*`` calls
    are additionally checked against ``repo`` (:func:`repo_path_refusal`); without a
    policy they are refused (fail closed).
    """

    async def guard(tool: str, inp: dict[str, Any], ctx: ToolPermissionContext) -> PermissionResult:
        if tool in allowed or tool == SKILL_TOOL:
            if tool in _REPO_SDK_TOOLS:
                if repo is None:
                    return _deny("Repository access is not available in this session.")
                reason = await asyncio.to_thread(repo_path_refusal, repo, tool, inp)
                if reason is not None:
                    return _deny(f"{_REPO_SDK_TOOLS[tool]} refused: {reason}")
            if on_allowed is not None:
                on_allowed(tool, inp, ctx)
            return PermissionResultAllow(updated_input=inp)
        if tool in RESEARCHER_TOOLS or tool in PARTS_TOOLS:
            return _deny(
                "You have no file or web tools. Call research_part to have the part-researcher "
                "look it up."
            )
        return _deny(f"Tool '{tool}' is not available in CopperChat.")

    return guard


def make_researcher_guard(
    project: Project, *, resolver: Resolver | None = None, write_folder: str | None = None
) -> CanUseTool:
    """Path and network rules for the part-researcher's tool calls (separate query).

    - WebSearch: allowed. WebFetch: http(s) URLs of public hosts only.
    - Read/Glob: only under ``docs/parts/`` and ``.kicad-agent/uploads/``.
    - Write/Edit: only ``docs/parts/<MPN>/<name>.md|.txt`` (not ``_uploads/``), never a
      ``notes.md`` whose front matter says ``user_edited: true``.
    - ``..`` components, foreign absolute paths, symlink escapes (of the target or of
      ``docs/parts`` itself) are refused; any other tool is refused.
    """
    base = project.dir
    read_roots = [parts_root(project), uploads_image_root(project)]
    write_roots = [parts_root(project)]

    async def guard(
        tool: str, inp: dict[str, Any], _ctx: ToolPermissionContext
    ) -> PermissionResult:
        if tool == "WebSearch" or tool in PARTS_TOOLS:
            return PermissionResultAllow(updated_input=inp)
        if tool == "WebFetch":
            # Known residual: the CLI resolves the host again when it fetches, so DNS
            # rebinding between this check and the fetch is not prevented (our own
            # download_datasheet additionally checks the connected peer address).
            reason = await url_refusal(str(inp.get("url") or ""), resolver=resolver)
            if reason is not None:
                return _deny(f"WebFetch refused: {reason}")
            return PermissionResultAllow(updated_input=inp)
        key = _PATH_KEY.get(tool)
        if key is None:
            return _deny(f"{tool} is not available to the part-researcher")
        raw = str(inp.get(key) or "")
        if tool == "Glob":
            pattern = str(inp.get("pattern") or "")
            if not pattern or pattern.startswith("/") or ".." in PurePosixPath(pattern).parts:
                return _deny("Glob pattern must be relative and must not contain '..'")
        if tool in ("Read", "Glob"):
            if resolve_inside(raw, base, read_roots) is None:
                return _deny("Read access is limited to docs/parts/ and .kicad-agent/uploads/")
            return PermissionResultAllow(updated_input=inp)
        return _check_write(project, raw, inp, write_roots, write_folder)

    return guard


def _check_write(
    project: Project,
    raw: str,
    inp: dict[str, Any],
    write_roots: list[Any],
    write_folder: str | None = None,
) -> PermissionResult:
    target = resolve_inside(raw, project.dir, write_roots)
    if target is None:
        return _deny("Writes are limited to docs/parts/<MPN>/notes.md")
    real_root = parts_root(project).resolve(strict=False)
    real_uploads = uploads_pdf_root(project).resolve(strict=False)
    if target == real_uploads or target.is_relative_to(real_uploads):
        return _deny("docs/parts/_uploads/ holds user uploads and is read-only")
    rel = target.relative_to(real_root).parts
    if len(rel) != 2 or not _WRITABLE_NAME.fullmatch(rel[1]):
        return _deny("Writes are limited to docs/parts/<MPN>/notes.md (or another .md/.txt file)")
    try:
        if mpn_dirname(rel[0]) != rel[0]:
            raise InvalidMpn(rel[0])
    except InvalidMpn:
        return _deny(f"{rel[0]!r} is not a valid part folder name")
    if write_folder is not None and rel[0].casefold() != write_folder.casefold():
        return _deny(f"This research may only write docs/parts/{write_folder}/")
    if target.name == "notes.md" and notes_user_edited(target):
        return _deny("notes.md is user-edited (user_edited: true); do not modify it")
    return PermissionResultAllow(updated_input=inp)


def guard_as_pre_tool_use_hook(guard: CanUseTool) -> HookCallback:
    """Run ``guard`` as a ``PreToolUse`` hook (deny only).

    The CLI auto-approves some calls without asking ``can_use_tool`` — notably Read/Glob
    of paths inside ``cwd`` (review of PR #11, confirmed live). A PreToolUse hook fires for
    every tool call, so the researcher's rules are enforced here; ``can_use_tool`` stays
    set for the calls the CLI does ask about. An allowed call returns ``{}`` (no opinion).
    """

    async def hook(
        input_data: HookInput, tool_use_id: str | None, _context: HookContext
    ) -> HookJSONOutput:
        raw: dict[str, Any] = dict(input_data)
        tool = str(raw.get("tool_name") or "")
        tool_input = raw.get("tool_input")
        inp = dict(tool_input) if isinstance(tool_input, dict) else {}
        res = await guard(tool, inp, ToolPermissionContext(tool_use_id=tool_use_id))
        if isinstance(res, PermissionResultDeny):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": res.message,
                }
            }
        return {}

    return hook
