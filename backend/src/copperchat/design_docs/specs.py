"""Tool provider ``copperchat.design_docs.specs``: the five doc tools (spec §7.4).

All of them are KiCad-less (``requires_kicad=False``) and ``mutating=False``: they never
trigger the KiCad turn snapshot, the plan gate or the change budget. The write policy of
the session type is enforced inside ``DesignDocs``.
"""

from __future__ import annotations

from typing import Any

from copperchat.design_docs.paths import DocError
from copperchat.design_docs.service import (
    MAX_READ_BYTES,
    ConversationInfo,
    DesignDocs,
    DocWriteResult,
    WritePolicy,
    policy_for,
)
from copperchat.tools import StructuredToolError, ToolContext, ToolResult, ToolSpec

_PATH = {
    "type": "string",
    "description": "docs/design/<name>.md (or the bare name of a fixed doc, e.g. requirements.md)",
    "minLength": 1,
    "maxLength": 200,
}
_SUMMARY = {
    "type": "string",
    "description": "One line: what you changed and why (shown in the doc history)",
    "minLength": 1,
    "maxLength": 200,
}
_BASE = {
    "type": ["string", "null"],
    "description": "The version from your last doc_read (null only to create a new doc)",
}


def _docs(ctx: ToolContext) -> DesignDocs:
    if ctx.docs is None:
        raise StructuredToolError(
            "Design docs are not available",
            "this CopperChat agent has no design-doc service",
            "Answer without the design docs and tell the user.",
        )
    return ctx.docs


def _conversation(ctx: ToolContext) -> ConversationInfo | None:
    if ctx.conversation_id is None:
        return None
    return ConversationInfo(
        id=ctx.conversation_id,
        seq=ctx.conversation_seq or 0,
        title=ctx.conversation_title or "Session",
        type=ctx.conversation_type or "unknown",
    )


def _policy(ctx: ToolContext) -> WritePolicy:
    return policy_for(ctx.conversation_type, ctx.conversation_id)


def _str(args: dict[str, Any], key: str, *, required: bool = True) -> Any:
    value = args.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise DocError(
            "invalid_input", f"{key} is missing or not a string", f"{key} is required", ""
        )
    return value


async def _notify(ctx: ToolContext, result: DocWriteResult) -> None:
    if ctx.post_doc_notice is None:
        return
    for notice in result.notices:
        await ctx.post_doc_notice(notice)


def _write_data(result: DocWriteResult, *, counts: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {"version": result.version, "revision_id": result.revision_id}
    if counts:
        data |= {"added": result.added, "removed": result.removed}
    return data


async def _doc_list(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return ToolResult(data=(await _docs(ctx).list(ctx.project)).snake())


async def _doc_read(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    section = _str(args, "section", required=False)
    doc = await _docs(ctx).read(ctx.project, _str(args, "path"), section)
    if len(doc.body.encode("utf-8")) > MAX_READ_BYTES:
        raise DocError(
            "doc_read_too_large",
            f"{doc.path} is too large to read at once",
            f"the body exceeds {MAX_READ_BYTES // 1024} KiB",
            "Read it by section: " + ", ".join(doc.headings[:60]),
            details={"headings": doc.headings},
        )
    data: dict[str, Any] = {
        "path": doc.path,
        "exists": doc.exists,
        "version": doc.version,
        "status": doc.status,
        "user_edited": doc.user_edited,
        "approved_version": doc.approved_version,
        "body": doc.body,
        "headings": doc.headings,
    }
    if doc.template is not None:
        data["template"] = doc.template
    return ToolResult(data=data)


async def _doc_write(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    result = await _docs(ctx).write(
        ctx.project,
        _str(args, "path"),
        _str(args, "body"),
        _str(args, "base_version", required=False),
        author="agent",
        summary=_str(args, "summary"),
        conversation=_conversation(ctx),
        run_id=ctx.run_id,
        policy=_policy(ctx),
    )
    await _notify(ctx, result)
    return ToolResult(data=_write_data(result))


async def _doc_patch_section(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    create = args.get("create", False)
    if not isinstance(create, bool):
        raise DocError("invalid_input", "create must be a boolean", "got another type", "")
    result = await _docs(ctx).patch_section(
        ctx.project,
        _str(args, "path"),
        _str(args, "heading"),
        _str(args, "body"),
        _str(args, "base_version", required=False),
        mode=_str(args, "mode", required=False) or "replace",
        create=create,
        summary=_str(args, "summary"),
        conversation=_conversation(ctx),
        run_id=ctx.run_id,
        policy=_policy(ctx),
    )
    await _notify(ctx, result)
    return ToolResult(data=_write_data(result))


async def _doc_append(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    result = await _docs(ctx).append(
        ctx.project,
        _str(args, "path"),
        _str(args, "text"),
        summary=_str(args, "summary"),
        section=_str(args, "section", required=False),
        title=_str(args, "title", required=False),
        base_version=_str(args, "base_version", required=False),
        conversation=_conversation(ctx),
        run_id=ctx.run_id,
        policy=_policy(ctx),
    )
    await _notify(ctx, result)
    data = _write_data(result, counts=False)
    if result.stale_base:
        data["stale_base"] = True
    return ToolResult(data=data)


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
        "doc_list",
        "List the design documents under docs/design/ (requirements, architecture, parts, "
        "decisions, board and extra docs): status, version, sections, open points, last "
        "change. Files that are not read (symlinks, too large, ...) are listed as ignored.",
        {},
        [],
        _doc_list,
    ),
    _spec(
        "doc_read",
        "Read one design document (body without front matter) and its version. Give "
        "`section` to read one section by heading (e.g. 'Power' or 'Power/Rails'). A "
        "missing fixed doc returns its template.",
        {"path": _PATH, "section": {"type": "string", "maxLength": 200}},
        ["path"],
        _doc_read,
    ),
    _spec(
        "doc_write",
        "Replace the whole body of a design document (or create it with base_version "
        "null). Refused on decisions.md (append-only) and on docs the user edited - patch "
        "sections of those instead. Send the body only, never a '---' front matter block.",
        {
            "path": _PATH,
            "body": {"type": "string", "maxLength": 65536},
            "base_version": _BASE,
            "summary": _SUMMARY,
        },
        ["path", "body", "base_version", "summary"],
        _doc_write,
    ),
    _spec(
        "doc_patch_section",
        "Replace (mode 'replace') or extend (mode 'append') one section of a design "
        "document, addressed by its heading ('Power', or 'Parent/Child' when the heading "
        "is not unique). create=true adds a missing section as a level-2 heading. The body "
        "must not contain headings at or above the section's level.",
        {
            "path": _PATH,
            "heading": {"type": "string", "minLength": 1, "maxLength": 200},
            "body": {"type": "string", "maxLength": 65536},
            "base_version": _BASE,
            "summary": _SUMMARY,
            "mode": {"type": "string", "enum": ["replace", "append"]},
            "create": {"type": "boolean"},
        },
        ["path", "heading", "body", "base_version", "summary"],
        _doc_patch_section,
    ),
    _spec(
        "doc_append",
        "Append text to the end of a design document, or to the end of one `section`. "
        "For decisions.md give a `title` (no section): the server writes a dated entry "
        "with your session. base_version is optional; a stale one still appends and "
        "returns stale_base=true.",
        {
            "path": _PATH,
            "text": {"type": "string", "minLength": 1, "maxLength": 8192},
            "summary": _SUMMARY,
            "section": {"type": "string", "maxLength": 200},
            "title": {"type": "string", "minLength": 1, "maxLength": 120},
            "base_version": {"type": "string"},
        },
        ["path", "text", "summary"],
        _doc_append,
    ),
]
