"""Permission guards (``can_use_tool``) of the main agent and the part-researcher."""

from __future__ import annotations

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

from copperchat.parts import InvalidMpn, mpn_dirname, notes_user_edited
from copperchat.projects import Project
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


def _deny(msg: str) -> PermissionResult:
    return PermissionResultDeny(message=msg, interrupt=False)


def make_main_guard(
    allowed: frozenset[str], *, on_allowed: ToolCallObserver | None = None
) -> CanUseTool:
    """The main agent's guard: exactly the registered CopperChat MCP tools plus ``Skill``.

    ``on_allowed`` sees every allowed call before it runs; the SDK backend uses it to learn
    the ``tool_use_id`` of a ``research_part`` call (the MCP handler itself does not get it).
    The researcher's own tools are refused here with a hint to delegate.
    """

    async def guard(tool: str, inp: dict[str, Any], ctx: ToolPermissionContext) -> PermissionResult:
        if tool in allowed or tool == SKILL_TOOL:
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
