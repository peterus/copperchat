import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from copperchat.agent.sdk_tools import (
    build_sdk_tool_list,
    build_sdk_tools,
    mcp_result_to_event,
    sdk_tool_name,
)
from copperchat.kicad_session import KicadUnavailable
from copperchat.projects import Project
from copperchat.tools import ImageBlock, StructuredToolError, ToolContext, ToolResult, ToolSpec

SCHEMA = {"type": "object", "properties": {}}


def _project(tmp_path: Path) -> Project:
    return Project(
        id="a1b2c3d4e5f6",
        name="demo",
        rel_path="demo/demo.kicad_pro",
        dir=tmp_path,
        pro_file=tmp_path / "demo.kicad_pro",
        sch_file=None,
        pcb_file=None,
    )


@pytest.fixture
def calls() -> list[Any]:
    return []


@pytest.fixture
def ctx_factory(tmp_path: Path, calls: list[Any]):
    def make() -> ToolContext:
        async def on_mutation() -> None:
            calls.append("mutation")

        return ToolContext(
            project=_project(tmp_path),
            session=cast(Any, None),
            settings=cast(Any, None),
            on_mutation=on_mutation,
            on_documents_changed=lambda docs: calls.append(("changed", docs)),
            on_kicad_unavailable=lambda exc: calls.append(("kicad", str(exc))),
        )

    return make


def _spec(handler, **kw) -> ToolSpec:  # type: ignore[no-untyped-def]
    return ToolSpec(
        name=kw.pop("name", "probe"),
        description="d",
        input_schema=SCHEMA,
        handler=handler,
        **kw,
    )


def _tool(spec: ToolSpec, ctx_factory):  # type: ignore[no-untyped-def]
    [t] = build_sdk_tool_list([spec], ctx_factory)
    return t


async def test_success_returns_json_text_and_image_blocks(ctx_factory):
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        return ToolResult(
            data={"echo": args["x"]}, images=[ImageBlock(mime="image/png", data=b"PNG")]
        )

    raw = await _tool(_spec(handler), ctx_factory).handler({"x": 1})
    assert raw.get("is_error", False) is False
    assert json.loads(raw["content"][0]["text"]) == {"echo": 1}
    assert raw["content"][1] == {"type": "image", "data": "UE5H", "mimeType": "image/png"}


async def test_no_data_reports_ok(ctx_factory):
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        return ToolResult()

    raw = await _tool(_spec(handler), ctx_factory).handler({})
    assert json.loads(raw["content"][0]["text"]) == {"ok": True}


async def test_structured_error_becomes_is_error_json(ctx_factory):
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        raise StructuredToolError(
            "no such ref", "U99 is not in the schematic", "list symbols first"
        )

    raw = await _tool(_spec(handler), ctx_factory).handler({})
    assert raw["is_error"] is True
    assert json.loads(raw["content"][0]["text"]) == {
        "error": {
            "what": "no such ref",
            "why": "U99 is not in the schematic",
            "suggestion": "list symbols first",
        }
    }


async def test_kicad_unavailable_reports_to_context(ctx_factory, calls):
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        raise KicadUnavailable("api-server timed out")

    raw = await _tool(_spec(handler), ctx_factory).handler({})
    assert raw["is_error"] is True
    assert "KiCad" in json.loads(raw["content"][0]["text"])["error"]["what"]
    assert calls == [("kicad", "api-server timed out")]


async def test_unexpected_exception_is_a_tool_error(ctx_factory):
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        raise ValueError("boom")

    raw = await _tool(_spec(handler), ctx_factory).handler({})
    assert raw["is_error"] is True
    assert "ValueError: boom" in json.loads(raw["content"][0]["text"])["error"]["why"]


async def test_cancellation_propagates_and_is_not_an_error_result(ctx_factory, calls):
    # Spike 1A (b2): interrupt() cancels the running in-process MCP tool coroutine.
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _tool(_spec(handler), ctx_factory).handler({})
    assert calls == []


async def test_mutating_tool_calls_on_mutation_first_and_reports_changed_docs(ctx_factory, calls):
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        calls.append("handler")
        return ToolResult(data={"ok": True}, changed_docs={"sch"})

    spec = _spec(handler, mutating=True, changes_documents=True)
    await _tool(spec, ctx_factory).handler({})
    assert calls == ["mutation", "handler", ("changed", {"sch"})]


async def test_non_mutating_tool_skips_on_mutation(ctx_factory, calls):
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        calls.append("handler")
        return ToolResult()

    await _tool(_spec(handler), ctx_factory).handler({})
    assert calls == ["handler"]


async def test_context_factory_failure_is_a_tool_error(tmp_path):
    def broken() -> ToolContext:
        raise KicadUnavailable("no session")

    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        raise AssertionError("must not run")

    raw = await _tool(_spec(handler), broken).handler({})
    assert raw["is_error"] is True


def test_tool_context_new_fields_default_to_none(tmp_path):
    ctx = ToolContext(project=_project(tmp_path), session=cast(Any, None), settings=cast(Any, None))
    assert ctx.user_message is None
    assert ctx.on_documents_changed is None and ctx.on_kicad_unavailable is None
    assert ToolResult().changed_docs == set()


def test_names_and_server_config(ctx_factory):
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        return ToolResult()

    assert sdk_tool_name("run_erc") == "mcp__copperchat__run_erc"
    server = build_sdk_tools([_spec(handler)], ctx_factory)
    assert server["type"] == "sdk" and server["name"] == "copperchat"


def test_mcp_result_to_event_roundtrip():
    ev = mcp_result_to_event(
        "t1",
        {
            "content": [
                {"type": "text", "text": "a"},
                {"type": "image", "data": "UE5H", "mimeType": "image/png"},
            ],
            "is_error": True,
        },
    )
    assert ev.id == "t1" and ev.content == "a" and ev.is_error is True
    assert ev.images[0].data == b"PNG"
