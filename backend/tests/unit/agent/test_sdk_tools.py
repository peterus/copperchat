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


async def test_footprint_leftovers_reach_the_agent(ctx_factory):
    """A failed footprint compensation (DocumentInconsistent with leftovers, spec §9.3):
    the agent sees what was left behind, per kind (a replaced footprint must not be
    deleted), that the schematic is not affected, and the notes the call carried."""
    from copperchat.errors import DocumentInconsistent, attach_tool_notes

    seen: list[Exception] = []
    note = {"kind": "pending_cleaned", "lib_id": "L:Old", "message": "Removed L:Old"}

    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        exc = DocumentInconsistent(
            "undoing the partial fp_create of L:A failed: boom; left behind: L:A, L:B, row L",
            leftovers=["L:A", "L:B", "row L"],
            replaced=["L:B"],
            footprint_only=True,
        )
        attach_tool_notes(exc, [note])
        raise exc

    def make() -> ToolContext:
        ctx = ctx_factory()
        ctx.on_document_inconsistent = seen.append
        return ctx

    raw = await _tool(_spec(handler, name="fp_create"), make).handler({"lib_id": "L:A"})
    assert raw["is_error"] is True
    err = json.loads(raw["content"][0]["text"])["error"]
    assert err["code"] == "document_inconsistent"
    assert err["details"]["leftovers"] == ["L:A", "L:B", "row L"]
    assert err["details"]["notes"] == [note]
    assert "the schematic is not affected" in err["why"]
    assert "L:A, L:B, row L" in err["why"]
    assert "removes L:A, row L" in err["suggestion"]
    assert "L:B is an existing footprint of the user" in err["suggestion"]
    assert "Do not delete" in err["suggestion"]
    assert "fp_create L:A" in err["what"]
    assert len(seen) == 1 and isinstance(seen[0], DocumentInconsistent)


@pytest.mark.parametrize("partial", [False, True])
async def test_schematic_inconsistent_with_row_leftover_keeps_schematic_text(
    ctx_factory, partial: bool
):
    """A schematic `DocumentInconsistent` that got a `library_row` leftover appended
    (the row removal failed too): the agent still hears that the design is reloaded from
    its last save (and the `last_save_partial` variant), plus what was left behind."""
    from copperchat.errors import DocumentInconsistent

    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        raise DocumentInconsistent(
            "the edit could not be undone", last_save_partial=partial, leftovers=["row L"]
        )

    raw = await _tool(_spec(handler, name="sch_set_fields"), ctx_factory).handler({"ref": "R1"})
    err = json.loads(raw["content"][0]["text"])["error"]
    assert err["code"] == "document_inconsistent"
    assert "reloads the design from its last save" in err["why"]
    assert ("may still contain part of this edit" in err["why"]) is partial
    assert "These remain in the local footprint library: row L." in err["why"]
    assert "schematic is not affected" not in err["why"]
    kept = "check this edit's part" if partial else "Only this edit is lost"
    assert kept in err["suggestion"]
    assert "left behind (row L)" in err["suggestion"]
    assert "Undoing this turn in History removes row L" in err["suggestion"]
    assert err["details"]["leftovers"] == ["row L"]


async def test_board_leftovers_are_worded_as_board_items(ctx_factory):
    """A failed board compensation (SP4 P1a residual): the leftovers are items on the
    board, or the board file the call created - never the footprint library."""
    from copperchat.errors import DocumentInconsistent

    left = ["extra track 1111", "missing footprint 2222"]

    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        raise DocumentInconsistent("undoing the partial pcb_move change failed", leftovers=left)

    raw = await _tool(_spec(handler, name="pcb_move"), ctx_factory).handler({})
    err = json.loads(raw["content"][0]["text"])["error"]
    assert err["code"] == "document_inconsistent"
    assert "footprint library" not in err["why"] + err["suggestion"]
    assert "reloads the design from its last save" in err["why"]
    assert (
        "These board items differ from the state before this edit: extra track 1111, "
        "missing footprint 2222." in err["why"]
    )
    assert "left behind (extra track 1111, missing footprint 2222)" in err["suggestion"]
    assert "fp_delete" not in err["suggestion"]
    assert err["details"]["leftovers"] == left

    async def created(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        raise DocumentInconsistent("the board could not be removed", leftovers=["demo.kicad_pcb"])

    spec = _spec(created, name="pcb_sync_from_schematic")
    err = json.loads((await _tool(spec, ctx_factory).handler({}))["content"][0]["text"])["error"]
    assert "footprint library" not in err["why"] + err["suggestion"]
    assert "The board file this call created is still there: demo.kicad_pcb." in err["why"]
    assert "Undoing this turn in History removes demo.kicad_pcb" in err["suggestion"]
