"""Adapter from the provider-neutral ToolSpec registry to in-process SDK MCP tools."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server

from copperchat.agent.events import ToolResultEvt
from copperchat.errors import DocumentInconsistent
from copperchat.kicad_session import KicadUnavailable
from copperchat.kicad_tools.edit.engine import save_after_edit
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
        state = editing.turn(ctx.project.id, ctx.run_id)
        if state.halted:
            raise StructuredToolError(
                f"{spec.name} refused: an earlier edit of this turn failed",
                _HALTED_WHY.get(state.halted_by, _HALTED_WHY["document_inconsistent"]),
                "Stop editing; tell the user what happened.",
                code=state.halted_by,
            )
        return await _two_phase_steps(spec, ctx, args)


_HALTED_WHY = {
    "document_inconsistent": "It could not be undone exactly; this turn ends and the design "
    "is reloaded from its last save",
    "save_failed": "Saving it failed; this turn ends and CopperChat tries the save again",
}


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
    generation = getattr(ctx.session, "generation", None)
    result = await spec.apply(ctx, cs)
    if getattr(ctx.session, "generation", None) != generation:
        # Restarted after the edit (e.g. a parallel read tool timed out): the server
        # holds the last save, the edit is gone - a KiCad failure, never a success.
        raise KicadUnavailable(
            "the KiCad api-server restarted right after the edit; it holds the last save"
        )
    if gate is not None:
        try:  # the change is applied and verified: bookkeeping never fails the call
            await gate.record(ctx, cs)
            if result.data is not None and "budget" in result.data:
                result.data["budget"] = await gate.budget_info(ctx)
        except Exception:
            log.exception("recording the budget of %s failed", spec.name)
    await _save_edit(spec, ctx, result)
    return result


async def _save_edit(spec: ToolSpec, ctx: ToolContext, result: ToolResult) -> None:
    """Every successful edit is saved at once (#6). A failing save is never swallowed:
    the call fails with `save_failed`, further edits of the turn are refused and the
    run engine ends the turn; the turn-end autosave tries again (the change is in
    KiCad's memory, so the changed documents are still reported to the turn)."""
    try:
        await save_after_edit(ctx, result.changed_docs)
    except (asyncio.CancelledError, KicadUnavailable):
        raise  # KicadUnavailable: the server restarted from the last save (handler)
    except Exception as exc:
        log.warning("saving after %s failed: %s", spec.name, exc)
        if ctx.editing is not None:
            state = ctx.editing.turn(ctx.project.id, ctx.run_id)
            state.halted, state.halted_by = True, "save_failed"
        if result.changed_docs and ctx.on_documents_changed is not None:
            ctx.on_documents_changed(set(result.changed_docs))
        if ctx.on_save_failed is not None:
            ctx.on_save_failed(exc)
        raise StructuredToolError(
            f"{spec.name} was applied, but saving the design afterwards failed",
            str(exc) or type(exc).__name__,
            "This turn ends; CopperChat tries the save again. Tell the user the edit is "
            "in KiCad but may not be on disk yet, and do not edit further in this turn.",
            code="save_failed",
        ) from exc


_TARGET_KEYS = ("items", "pins", "pin", "at", "from", "to", "ref", "net", "old", "new",
                "lib_id", "name")  # fmt: skip


def _target(args: dict[str, Any]) -> str:
    """What a tool call acted on, for a message ("#FLG02", "R1.1 R2.1"): the string
    arguments that name items, pins or nets."""
    parts: list[str] = []
    for key in _TARGET_KEYS:
        value = args.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            parts.append(", ".join(value))
    text = " ".join(parts)
    return text if len(text) <= 80 else text[:77] + "..."


def _inconsistent_error(tool: str, args: dict[str, Any], exc: Exception) -> StructuredToolError:
    """Plain words for the agent to pass on (#6b, spec §5.4): what failed, what the
    design is reloaded to, that earlier edits are kept; the internal reason in details."""
    target = _target(args)
    edit = f"{tool} {target}" if target else tool
    if getattr(exc, "last_save_partial", False):
        why = (
            "Undoing it did not bring the design back exactly. When this turn ends, "
            "CopperChat reloads the design from its last save, which may still contain "
            "part of this edit."
        )
        kept = "The earlier edits of this turn are saved and kept; check this edit's part."
    else:
        why = (
            "Undoing it did not bring the design back exactly. When this turn ends, "
            "CopperChat reloads the design from its last save: the state just before "
            "this edit."
        )
        kept = "Only this edit is lost; the earlier edits of this turn are saved and kept."
    return StructuredToolError(
        f"{edit} failed and could not be undone exactly",
        why,
        f"{kept} Stop editing, explain this to the user (what you tried, that it was "
        "rolled back, what is kept) and do not retry in this turn.",
        code="document_inconsistent",
        details={"detail": str(exc)},
    )


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
            return _error(_inconsistent_error(spec.name, args, exc))
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
