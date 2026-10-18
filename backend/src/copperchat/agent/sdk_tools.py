"""Adapter from the provider-neutral ToolSpec registry to in-process SDK MCP tools."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server

from copperchat.agent.events import ToolResultEvt
from copperchat.errors import DocumentInconsistent
from copperchat.kicad_session import KicadUnavailable
from copperchat.tools import ImageBlock, StructuredToolError, ToolContext, ToolResult, ToolSpec

log = logging.getLogger(__name__)

SERVER_NAME = "copperchat"

McpHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def sdk_tool_name(name: str) -> str:
    """Name under which the CLI exposes one of our tools (``mcp__<server>__<tool>``)."""
    return f"mcp__{SERVER_NAME}__{name}"


def _error(err: StructuredToolError) -> dict[str, Any]:
    text = json.dumps({"error": err.to_dict()}, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _success(result: ToolResult) -> dict[str, Any]:
    if result.text is not None:
        text = result.text
    else:
        payload = result.data if result.data is not None else {"ok": True}
        text = json.dumps(payload, ensure_ascii=False)
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for img in result.images:
        content.append(
            {
                "type": "image",
                "data": base64.b64encode(img.data).decode("ascii"),
                "mimeType": img.mime,
            }
        )
    return {"content": content}


async def _run_two_phase(spec: ToolSpec, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """E-C4 adapter order: read-only -> gate.before_prepare -> prepare -> check_budget ->
    on_mutation (turn snapshot) -> apply -> gate.record. Every refusal happens before
    the snapshot and before any write (owner rule "refusals never snapshot")."""
    assert spec.prepare is not None and spec.apply is not None
    if getattr(ctx.session, "read_only", False):
        raise StructuredToolError(
            f"{spec.name} cannot change a read-only project",
            "The project is open in KiCad on another machine or process (lock file present)",
            "Ask the user to close it there and reopen the project in CopperChat.",
            code="read_only",
        )
    editing = ctx.editing
    if editing is None:
        return await _two_phase_steps(spec, ctx, args)
    async with editing.edit_lock(ctx.project.id):
        if editing.turn(ctx.project.id, ctx.run_id).halted:
            raise StructuredToolError(
                f"{spec.name} refused: an earlier edit of this turn could not be undone",
                "The design is reloaded from its last save when this turn ends",
                "Stop editing; tell the user what happened.",
                code="document_inconsistent",
            )
        return await _two_phase_steps(spec, ctx, args)


async def _two_phase_steps(spec: ToolSpec, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    assert spec.prepare is not None and spec.apply is not None
    gate = ctx.editing.gate if ctx.editing is not None else None
    if gate is not None:
        await gate.before_prepare(ctx, spec.name)
    cs = await spec.prepare(ctx, args)
    if gate is not None:
        await gate.check_budget(ctx, cs)
    if ctx.on_mutation is not None:
        await ctx.on_mutation()
    result = await spec.apply(ctx, cs)
    if gate is not None:
        try:  # the change is applied and verified: bookkeeping never fails the call
            await gate.record(ctx, cs)
            if result.data is not None and "budget" in result.data:
                result.data["budget"] = await gate.budget_info(ctx)
        except Exception:
            log.exception("recording the budget of %s failed", spec.name)
    return result


def _make_handler(
    spec: ToolSpec,
    ctx_factory: Callable[[], ToolContext],
    kicadless_ctx_factory: Callable[[], ToolContext] | None = None,
) -> McpHandler:
    # asyncio.CancelledError (an interrupt cancels the running tool, spike 1A b2) is a
    # BaseException: it is deliberately not caught here and propagates to the SDK.
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            ctx = ctx_factory()
        except KicadUnavailable as exc:
            if not spec.requires_kicad and kicadless_ctx_factory is not None:
                ctx = kicadless_ctx_factory()
            else:
                return _error(
                    StructuredToolError(
                        "KiCad is not available",
                        str(exc),
                        "Ask the user to reopen the project in CopperChat, then try again.",
                    )
                )
        editing = getattr(ctx, "editing", None)  # test doubles may omit SP2 fields
        if editing is not None:
            editing.note_call(ctx, spec.name, args)
        try:
            if spec.prepare is not None:
                result = await _run_two_phase(spec, ctx, args)
            else:
                if spec.mutating and ctx.on_mutation is not None:
                    await ctx.on_mutation()
                result = await spec.handler(ctx, args)
        except StructuredToolError as err:
            return _error(err)
        except DocumentInconsistent as exc:
            if editing is not None:
                editing.turn(ctx.project.id, ctx.run_id).halted = True
            on_inconsistent = getattr(ctx, "on_document_inconsistent", None)
            if on_inconsistent is not None:
                on_inconsistent(exc)
            return _error(
                StructuredToolError(
                    f"{spec.name} failed and its partial change could not be undone exactly",
                    str(exc),
                    "This turn ends; the design is reloaded from its last save. Tell the "
                    "user what you tried and do not retry in this turn.",
                    code="document_inconsistent",
                )
            )
        except KicadUnavailable as exc:
            if ctx.on_kicad_unavailable is not None:
                ctx.on_kicad_unavailable(exc)
            return _error(
                StructuredToolError(
                    f"{spec.name} failed because KiCad stopped responding",
                    str(exc),
                    "The KiCad API server was restarted and this turn ends. Ask again to retry.",
                )
            )
        except Exception as exc:  # a bug in a tool must reach the agent as a clear error
            log.exception("tool %s failed", spec.name)
            return _error(
                StructuredToolError(
                    f"{spec.name} failed unexpectedly",
                    f"{type(exc).__name__}: {exc}",
                    "This is a CopperChat bug; do not retry the same call.",
                )
            )
        if result.changed_docs and ctx.on_documents_changed is not None:
            ctx.on_documents_changed(set(result.changed_docs))
        return _success(result)

    return handler


def build_sdk_tool_list(
    specs: Sequence[ToolSpec],
    ctx_factory: Callable[[], ToolContext],
    kicadless_ctx_factory: Callable[[], ToolContext] | None = None,
) -> list[SdkMcpTool[Any]]:
    return [
        SdkMcpTool(
            name=s.name,
            description=s.description,
            input_schema=s.input_schema,
            handler=_make_handler(s, ctx_factory, kicadless_ctx_factory),
        )
        for s in specs
    ]


def build_sdk_tools(
    specs: Sequence[ToolSpec],
    ctx_factory: Callable[[], ToolContext],
    kicadless_ctx_factory: Callable[[], ToolContext] | None = None,
) -> McpSdkServerConfig:
    """``kicadless_ctx_factory`` builds the context of ``requires_kicad=False`` tools when
    ``ctx_factory`` raises KicadUnavailable (no project open in KiCad)."""
    return create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=build_sdk_tool_list(specs, ctx_factory, kicadless_ctx_factory),
    )


def mcp_result_to_event(tool_use_id: str, raw: dict[str, Any]) -> ToolResultEvt:
    """Convert a raw MCP handler result (as built above) to a ToolResultEvt."""
    texts: list[str] = []
    images: list[ImageBlock] = []
    for item in raw.get("content", []):
        if item.get("type") == "text":
            texts.append(str(item.get("text", "")))
        elif item.get("type") == "image":
            images.append(
                ImageBlock(
                    mime=str(item.get("mimeType", "image/png")),
                    data=base64.b64decode(item.get("data", "")),
                )
            )
    return ToolResultEvt(
        id=tool_use_id,
        content="\n".join(texts),
        images=tuple(images),
        is_error=bool(raw.get("is_error", False)),
    )
