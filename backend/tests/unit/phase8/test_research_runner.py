import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from copperchat.agent.events import AgentEvent, SubagentText, ToolResultEvt, ToolUse
from copperchat.config import Settings
from copperchat.errors import StructuredToolError
from copperchat.projects import Project
from copperchat.research import runner
from copperchat.research.specs import TOOL_SPECS

ARGS = {"mpn": "TPS62133", "query": "How is it laid out?"}


def result(
    text: str = "TPS62133: 2.5 MHz buck. Notes: docs/parts/TPS62133/notes.md",
    is_error: bool = False,
    cost: float = 0.25,
) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=3,
        session_id="r",
        result=text,
        total_cost_usd=cost,
        usage={"input_tokens": 100, "output_tokens": 20},
        model_usage={
            "m": {
                "inputTokens": 120,
                "outputTokens": 30,
                "cacheReadInputTokens": 5,
                "cacheCreationInputTokens": 2,
            }
        },
    )


def steps() -> list[Any]:
    return [
        AssistantMessage(
            content=[
                TextBlock("Looking it up."),
                ToolUseBlock(id="r1", name="WebSearch", input={"query": "q"}),
            ],
            model="m",
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="r1", content="found", is_error=False)]),
        UserMessage(content="echoed prompt"),
        result(),
    ]


def make(
    project: Project, settings: Settings, query_fn: Any, **kw: Any
) -> tuple[runner.ResearchRunner, list[AgentEvent]]:
    out: list[AgentEvent] = []
    return runner.ResearchRunner(project, settings, emit=out.append, query_fn=query_fn, **kw), out


def test_options_are_isolated(project: Project, settings: Settings) -> None:
    opts = runner.researcher_options(project, settings, "TEMPLATE-MARKER")
    assert opts.tools == ["WebSearch", "WebFetch", "Read", "Write", "Edit", "Glob"]
    assert opts.allowed_tools == []  # every call through the guard
    assert opts.setting_sources == []
    assert opts.cwd == str(project.dir)
    assert opts.can_use_tool is not None
    assert set(opts.mcp_servers) == {"parts"}  # type: ignore[arg-type]
    assert opts.env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert opts.max_turns == 40 and opts.agents is None and not opts.skills
    assert opts.max_buffer_size is not None and opts.max_buffer_size >= 32 * 1024 * 1024
    assert isinstance(opts.system_prompt, str) and opts.system_prompt.endswith("TEMPLATE-MARKER")
    assert "Claude Code" not in opts.system_prompt


async def test_run_forwards_steps_under_the_call_and_collects_usage(
    project: Project, settings: Settings
) -> None:
    seen: dict[str, Any] = {}

    async def fake(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        seen["prompt"] = prompt
        assert options.can_use_tool is not None
        denied = await options.can_use_tool(
            "Write", {"file_path": "/etc/x", "content": ""}, ToolPermissionContext(tool_use_id="1")
        )
        seen["denied"] = isinstance(denied, PermissionResultDeny)
        for m in steps():
            yield m

    r, out = make(project, settings, fake)
    r.note_call(dict(ARGS), "toolu_main")
    text = await r.run(dict(ARGS))
    assert text.startswith("TPS62133")
    assert seen["denied"] is True
    assert "docs/parts/TPS62133/" in seen["prompt"] and "How is it laid out?" in seen["prompt"]
    assert out == [
        SubagentText(parent_tool_use_id="toolu_main", text="Looking it up."),
        ToolUse(id="r1", name="WebSearch", input={"query": "q"}, parent_tool_use_id="toolu_main"),
        ToolResultEvt(id="r1", content="found", parent_tool_use_id="toolu_main"),
    ]
    usage = r.take_usage()
    assert usage is not None and usage.input_tokens == 120 and usage.cost_usd == 0.25
    assert r.take_usage() is None


async def test_identical_calls_get_their_own_ids_in_order(
    project: Project, settings: Settings
) -> None:
    async def fake(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        yield AssistantMessage(content=[TextBlock("x")], model="m")
        yield result()

    r, out = make(project, settings, fake)
    r.note_call(dict(ARGS), "a")
    r.note_call(dict(ARGS), "b")
    await r.run(dict(ARGS))
    await r.run(dict(ARGS))
    assert [e.parent_tool_use_id for e in out] == ["a", "b"]  # type: ignore[union-attr]
    assert r.take_usage().cost_usd == 0.5  # type: ignore[union-attr]


async def test_unrecorded_call_runs_without_forwarding(
    project: Project, settings: Settings
) -> None:
    async def fake(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        for m in steps():
            yield m

    r, out = make(project, settings, fake)
    assert (await r.run(dict(ARGS))).startswith("TPS62133") and out == []


async def test_attachment_paths_validated(project: Project, settings: Settings) -> None:
    async def never(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        raise AssertionError("must not run")
        yield

    r, _ = make(project, settings, never)
    (project.dir / "proj.kicad_sch").write_text("x")
    for paths in (
        ["../../etc/passwd"],
        ["proj.kicad_sch"],
        ["docs/parts/_uploads/missing.pdf"],
        "docs/parts/x",
        ["a"] * 11,
    ):
        with pytest.raises(StructuredToolError):
            await r.run({**ARGS, "attachment_paths": paths})
    for bad in (
        {"mpn": "../x", "query": "q"},
        {"mpn": "X", "query": " "},
        {"query": "q"},
        {"mpn": "X", "query": "q" * 5000},
    ):
        with pytest.raises(StructuredToolError):
            await r.run(bad)


async def test_attachment_paths_reach_the_prompt(project: Project, settings: Settings) -> None:
    pdf = project.dir / "docs/parts/_uploads/abcd1234-ds.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.7\n")
    seen: list[str] = []

    async def fake(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        seen.append(prompt)
        yield result()

    r, _ = make(project, settings, fake)
    await r.run({**ARGS, "attachment_paths": [str(pdf)]})
    assert "- docs/parts/_uploads/abcd1234-ds.pdf" in seen[0]


async def test_error_result_raises_structured_error_but_counts_usage(
    project: Project, settings: Settings
) -> None:
    async def failing(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        yield result("rate limited", is_error=True)

    r, _ = make(project, settings, failing)
    with pytest.raises(StructuredToolError) as e:
        await r.run(dict(ARGS))
    assert e.value.to_dict()["what"] == "Part research failed"
    assert r.take_usage() is not None

    async def empty(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        return
        yield

    r2, _ = make(project, settings, empty)
    with pytest.raises(StructuredToolError):
        await r2.run(dict(ARGS))


async def test_timeout(project: Project, settings: Settings) -> None:
    async def slow(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        await asyncio.sleep(10)
        yield result()

    r, _ = make(project, settings, slow, timeout_s=0.05)
    with pytest.raises(StructuredToolError) as e:
        await r.run(dict(ARGS))
    assert "timed out" in e.value.what


async def test_cancel_all_stops_the_query(project: Project, settings: Settings) -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    async def hanging(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        try:
            started.set()
            await asyncio.sleep(30)
            yield result()
        finally:
            closed.set()

    r, _ = make(project, settings, hanging)
    task = asyncio.create_task(r.run(dict(ARGS)))
    await asyncio.wait_for(started.wait(), 2)
    r.cancel_all()
    with pytest.raises(StructuredToolError) as e:
        await asyncio.wait_for(task, 2)
    assert "cancelled" in e.value.what
    assert closed.is_set()


async def test_cancelling_the_handler_cancels_the_query(
    project: Project, settings: Settings
) -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    async def hanging(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        try:
            started.set()
            await asyncio.sleep(30)
            yield result()
        finally:
            closed.set()

    r, _ = make(project, settings, hanging)
    task = asyncio.create_task(r.run(dict(ARGS)))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()  # what an SDK interrupt does to a running MCP tool (spike 1A b2)
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(closed.wait(), 2)


async def test_same_part_is_serialised_other_parts_run_in_parallel(
    project: Project, settings: Settings
) -> None:
    active: dict[str, int] = {}
    peak: dict[str, int] = {}
    overall = 0
    overall_peak = 0

    async def fake(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        nonlocal overall, overall_peak
        key = "LM358" if "LM358" in prompt else "TPS62133"
        active[key] = active.get(key, 0) + 1
        peak[key] = max(peak.get(key, 0), active[key])
        overall += 1
        overall_peak = max(overall_peak, overall)
        await asyncio.sleep(0.05)
        active[key] -= 1
        overall -= 1
        yield result()

    r, _ = make(project, settings, fake)
    await asyncio.gather(
        r.run(dict(ARGS)), r.run(dict(ARGS)), r.run({"mpn": "LM358", "query": "q"})
    )
    assert peak == {"TPS62133": 1, "LM358": 1} and overall_peak == 2


async def test_usage_is_counted_when_the_query_is_cancelled(
    project: Project, settings: Settings
) -> None:
    started = asyncio.Event()

    async def hanging(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        usage = {"input_tokens": 50, "output_tokens": 7}
        for block in (TextBlock("a"), TextBlock("b")):  # one API message, two blocks
            yield AssistantMessage(content=[block], model="m", message_id="msg1", usage=usage)
        started.set()
        await asyncio.sleep(30)
        yield result()

    r, _ = make(project, settings, hanging)
    task = asyncio.create_task(r.run(dict(ARGS)))
    await asyncio.wait_for(started.wait(), 2)
    r.cancel_all()
    with pytest.raises(StructuredToolError):
        await asyncio.wait_for(task, 2)
    usage = r.take_usage()
    assert usage is not None and usage.input_tokens == 50 and usage.output_tokens == 7


async def test_end_turn_drops_unconsumed_calls(project: Project, settings: Settings) -> None:
    async def fake(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        yield AssistantMessage(content=[TextBlock("x")], model="m")
        yield result()

    r, out = make(project, settings, fake)
    r.note_call(dict(ARGS), "stale")  # allowed, but the handler never ran
    r.end_turn()
    r.note_call(dict(ARGS), "fresh")
    await r.run(dict(ARGS))
    assert [e.parent_tool_use_id for e in out] == ["fresh"]  # type: ignore[union-attr]


def test_tool_spec_registered() -> None:
    from copperchat.tools import registry

    spec = next(s for s in TOOL_SPECS if s.name == "research_part")
    assert spec.input_schema["required"] == ["mpn", "query"]
    assert set(spec.input_schema["properties"]) == {"mpn", "query", "attachment_paths"}
    assert not spec.mutating and not spec.changes_documents
    assert "research_part" in {s.name for s in registry.all_specs()}


async def test_tool_without_research_service_is_a_structured_error(
    project: Project, settings: Settings
) -> None:
    from copperchat.tools import ToolContext

    ctx = ToolContext(project=project, session=None, settings=settings)  # type: ignore[arg-type]
    with pytest.raises(StructuredToolError):
        await TOOL_SPECS[0].handler(ctx, dict(ARGS))


async def test_query_crash_is_a_structured_error(project: Project, settings: Settings) -> None:
    async def crashing(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        yield AssistantMessage(content=[TextBlock("x")], model="m")
        raise RuntimeError("buffer exceeded")

    r, _ = make(project, settings, crashing)
    with pytest.raises(StructuredToolError) as e:
        await r.run(dict(ARGS))
    assert e.value.what == "Part research failed" and "buffer exceeded" in e.value.why
