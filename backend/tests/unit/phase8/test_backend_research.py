"""SdkStreamingBackend + research_part end to end with a scripted SDK client (no model)."""

import asyncio
import base64
import io
from collections.abc import AsyncIterator
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from PIL import Image

from copperchat.agent import sdk_backend
from copperchat.agent.events import (
    AgentEvent,
    QueuedItem,
    SubagentText,
    ToolResultEvt,
    ToolUse,
    TurnFinished,
    Usage,
)
from copperchat.agent.protocol import AttachmentRef
from copperchat.agent.sdk_tools import build_sdk_tool_list
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.db.repos.uploads import UploadRow, insert_upload
from copperchat.projects import Project
from copperchat.tools import ToolContext, registry
from copperchat.uploads import store_upload

RP = "mcp__copperchat__research_part"
ARGS = {"mpn": "TPS62133", "query": "Layout?"}


def result(cost: float, *, session: str = "s1", **kw: Any) -> ResultMessage:
    base: dict[str, Any] = dict(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=session,
        total_cost_usd=cost,
        result="done",
        usage={"input_tokens": 10, "output_tokens": 2},
    )
    base.update(kw)
    return ResultMessage(**base)


class ScriptedClient:
    """Plays one main turn per prompt: research_part call → guard → handler → result."""

    def __init__(self, options: ClaudeAgentOptions, handler: Any, args: dict[str, Any]) -> None:
        self.options = options
        self.handler = handler
        self.args = args
        self.prompts: list[dict[str, Any]] = []
        self.out: asyncio.Queue[Any] = asyncio.Queue()
        self.interrupts = 0
        self.disconnected = False
        self.tool_result: dict[str, Any] | None = None

    async def connect(self, prompt: Any) -> None:
        async def pump() -> None:
            async for p in prompt:
                self.prompts.append(p)
                await self.out.put(
                    AssistantMessage(
                        content=[ToolUseBlock(id="toolu_rp", name=RP, input=dict(self.args))],
                        model="m",
                    )
                )
                assert self.options.can_use_tool is not None
                await self.options.can_use_tool(
                    RP, dict(self.args), ToolPermissionContext(tool_use_id="toolu_rp")
                )
                self.tool_result = await self.handler(dict(self.args))
                text = self.tool_result["content"][0]["text"]
                await self.out.put(
                    UserMessage(
                        content=[
                            ToolResultBlock(
                                tool_use_id="toolu_rp",
                                content=text,
                                is_error=bool(self.tool_result.get("is_error")),
                            )
                        ]
                    )
                )
                await self.out.put(result(0.5))
            await self.out.put(None)

        self.task = asyncio.create_task(pump())

    async def receive_messages(self) -> AsyncIterator[Any]:
        while (msg := await self.out.get()) is not None:
            yield msg

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def disconnect(self) -> None:
        self.disconnected = True
        self.task.cancel()


def research_ok(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
    async def gen() -> AsyncIterator[Any]:
        yield AssistantMessage(
            content=[TextBlock("Searching."), ToolUseBlock(id="r1", name="WebSearch", input={})],
            model="m",
        )
        yield UserMessage(content=[ToolResultBlock(tool_use_id="r1", content="hit")])
        yield result(
            0.25,
            session="r",
            result="Summary: 2.5 MHz.",
            model_usage={"m": {"inputTokens": 100, "outputTokens": 40}},
        )

    return gen()


def make_backend(
    settings: Settings,
    project: Project,
    db: Database | None,
    query_fn: Any,
    args: dict[str, Any] = ARGS,
) -> tuple[sdk_backend.SdkStreamingBackend, list[ScriptedClient]]:
    clients: list[ScriptedClient] = []
    specs = registry.all_specs()

    def base_ctx() -> ToolContext:
        return ToolContext(project=project, session=None, settings=settings)  # type: ignore[arg-type]

    backend: sdk_backend.SdkStreamingBackend

    def factory(options: ClaudeAgentOptions) -> ScriptedClient:
        # The backend's own ctx_factory (it adds `research`) sits behind the MCP server; call
        # the same handler the server would run.
        spec = next(s for s in specs if s.name == "research_part")
        handler = build_sdk_tool_list([spec], backend.tool_ctx_factory)[0].handler
        c = ScriptedClient(options, handler, args)
        clients.append(c)
        return c

    backend = sdk_backend.SdkStreamingBackend(
        settings=settings,
        specs=specs,
        ctx_factory=base_ctx,
        db=db,
        client_factory=factory,
        research_query_fn=query_fn,
    )
    return backend, clients


async def one_item(
    text: str = "hi", attachments: tuple[AttachmentRef, ...] = ()
) -> AsyncIterator[QueuedItem]:
    yield QueuedItem(message_id=1, client_message_id="c1", text=text, attachments=attachments)


async def test_research_steps_nest_under_the_call_and_usage_is_summed(
    settings, project, db
) -> None:  # type: ignore[no-untyped-def]
    backend, clients = make_backend(settings, project, db, research_ok)
    events: list[AgentEvent] = [ev async for ev in backend.start(project, one_item(), None)]
    kinds = [type(e).__name__ for e in events if type(e).__name__ != "SessionId"]
    assert kinds == [
        "ToolUse",
        "SubagentText",
        "ToolUse",
        "ToolResultEvt",
        "ToolResultEvt",
        "Usage",
        "TurnFinished",
    ]
    main = [e for e in events if isinstance(e, ToolUse) and e.parent_tool_use_id is None]
    assert main[0].id == "toolu_rp" and main[0].input == ARGS
    subs = [e for e in events if getattr(e, "parent_tool_use_id", None) == "toolu_rp"]
    assert subs == [
        SubagentText(parent_tool_use_id="toolu_rp", text="Searching."),
        ToolUse(id="r1", name="WebSearch", input={}, parent_tool_use_id="toolu_rp"),
        ToolResultEvt(id="r1", content="hit", parent_tool_use_id="toolu_rp"),
    ]
    final = next(e for e in events if isinstance(e, ToolResultEvt) and e.id == "toolu_rp")
    assert final.content == "Summary: 2.5 MHz." and not final.is_error
    usage = next(e for e in events if isinstance(e, Usage))
    assert usage.cost_usd == pytest.approx(0.75)  # main 0.5 + research 0.25
    assert usage.input_tokens == 10 + 100 and usage.output_tokens == 2 + 40
    assert events[-1] == TurnFinished(reason="completed")
    opts = clients[0].options
    assert opts.tools == ["Skill"] and opts.skills and opts.setting_sources == []
    assert opts.agents is None and "Agent" not in (opts.tools or [])
    assert opts.allowed_tools == []
    assert clients[0].disconnected


async def test_research_failure_is_a_tool_error_not_a_turn_error(settings, project, db) -> None:  # type: ignore[no-untyped-def]
    def failing(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            yield result(0.1, is_error=True, result="overloaded")

        return gen()

    backend, _ = make_backend(settings, project, db, failing)
    events = [ev async for ev in backend.start(project, one_item(), None)]
    final = next(e for e in events if isinstance(e, ToolResultEvt) and e.id == "toolu_rp")
    assert final.is_error and "Part research failed" in final.content
    assert events[-1] == TurnFinished(reason="completed")
    usage = next(e for e in events if isinstance(e, Usage))
    assert usage.cost_usd == pytest.approx(0.6)


async def test_invalid_mpn_is_refused_before_any_query(settings, project, db) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []

    def spy(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        calls.append(prompt)
        return research_ok(prompt, options)

    backend, _ = make_backend(settings, project, db, spy, args={"mpn": "../etc", "query": "q"})
    events = [ev async for ev in backend.start(project, one_item(), None)]
    final = next(e for e in events if isinstance(e, ToolResultEvt) and e.id == "toolu_rp")
    assert final.is_error and "Invalid part number" in final.content and calls == []


async def test_interrupt_cancels_the_research_query(settings, project, db) -> None:  # type: ignore[no-untyped-def]
    started = asyncio.Event()
    closed = asyncio.Event()

    def hanging(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            try:
                yield AssistantMessage(content=[TextBlock("Working.")], model="m")
                started.set()
                await asyncio.sleep(30)
            finally:
                closed.set()

        return gen()

    backend, clients = make_backend(settings, project, db, hanging)
    events: list[AgentEvent] = []

    async def consume() -> None:
        async for ev in backend.start(project, one_item(), None):
            events.append(ev)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 2)
    await backend.interrupt()
    await asyncio.wait_for(task, 5)
    assert closed.is_set() and clients[0].interrupts == 1
    final = next(e for e in events if isinstance(e, ToolResultEvt) and e.id == "toolu_rp")
    assert final.is_error and "cancelled" in final.content
    assert SubagentText(parent_tool_use_id="toolu_rp", text="Working.") in events


async def test_closing_the_stream_cancels_running_research(settings, project, db) -> None:  # type: ignore[no-untyped-def]
    started = asyncio.Event()
    closed = asyncio.Event()

    def hanging(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            try:
                started.set()
                await asyncio.sleep(30)
                yield result(0)
            finally:
                closed.set()

        return gen()

    backend, _ = make_backend(settings, project, db, hanging)
    gen = backend.start(project, one_item(), None)
    first = asyncio.ensure_future(gen.__anext__())
    await asyncio.wait_for(started.wait(), 2)
    await first  # the research_part ToolUse
    await gen.aclose()
    await asyncio.wait_for(closed.wait(), 2)


async def test_sub_rows_arriving_before_their_call_are_held_back(settings, project) -> None:  # type: ignore[no-untyped-def]
    """Forwarded rows never precede the research_part ToolUse they belong to."""

    class Early(ScriptedClient):
        async def connect(self, prompt: Any) -> None:
            async def pump() -> None:
                async for _p in prompt:
                    assert self.options.can_use_tool is not None
                    await self.options.can_use_tool(
                        RP, dict(ARGS), ToolPermissionContext(tool_use_id="toolu_rp")
                    )
                    handler_task = asyncio.create_task(self.handler(dict(ARGS)))
                    await asyncio.sleep(0.05)  # research rows are emitted first
                    await self.out.put(
                        AssistantMessage(
                            content=[ToolUseBlock(id="toolu_rp", name=RP, input=dict(ARGS))],
                            model="m",
                        )
                    )
                    res = await handler_task
                    await self.out.put(
                        UserMessage(
                            content=[
                                ToolResultBlock(
                                    tool_use_id="toolu_rp", content=res["content"][0]["text"]
                                )
                            ]
                        )
                    )
                    await self.out.put(result(0.5))
                await self.out.put(None)

            self.task = asyncio.create_task(pump())

    specs = registry.all_specs()
    spec = next(s for s in specs if s.name == "research_part")
    backend: sdk_backend.SdkStreamingBackend

    def factory(options: ClaudeAgentOptions) -> ScriptedClient:
        handler = build_sdk_tool_list([spec], backend.tool_ctx_factory)[0].handler
        return Early(options, handler, ARGS)

    backend = sdk_backend.SdkStreamingBackend(
        settings=settings,
        specs=specs,
        ctx_factory=lambda: ToolContext(project=project, session=None, settings=settings),  # type: ignore[arg-type]
        client_factory=factory,
        research_query_fn=research_ok,
    )
    events = [ev async for ev in backend.start(project, one_item(), None)]
    first_sub = next(i for i, e in enumerate(events) if getattr(e, "parent_tool_use_id", None))
    call = next(i for i, e in enumerate(events) if isinstance(e, ToolUse) and e.id == "toolu_rp")
    assert call < first_sub


async def test_attachments_become_content_blocks(settings, project, db) -> None:  # type: ignore[no-untyped-def]
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "PNG")
    refs = []
    for data, name in ((buf.getvalue(), "photo.png"), (b"%PDF-1.7\n", "ds.pdf")):
        up = store_upload(project, data, name)
        await insert_upload(
            db, UploadRow(up.id, project.id, up.kind, up.name, up.rel_path, up.mime, up.size, "t")
        )
        refs.append(AttachmentRef(id=up.id, kind=up.kind, name=up.name))
    backend, clients = make_backend(settings, project, db, research_ok)
    _ = [ev async for ev in backend.start(project, one_item("look", tuple(refs)), None)]
    content = clients[0].prompts[0]["message"]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["type"] == "image" and base64.b64decode(content[1]["source"]["data"])
    assert "docs/parts/_uploads/" in content[2]["text"] and "research_part" in content[2]["text"]


async def test_without_db_no_skills(settings, project) -> None:  # type: ignore[no-untyped-def]
    backend, clients = make_backend(settings, project, None, research_ok)
    _ = [ev async for ev in backend.start(project, one_item(), None)]
    assert clients[0].options.tools == [] and clients[0].options.plugins == []


async def test_research_works_without_kicad_and_interrupt_counts_usage(
    settings, project, db
) -> None:  # type: ignore[no-untyped-def]
    from copperchat.kicad_session import KicadUnavailable

    def no_kicad() -> ToolContext:
        raise KicadUnavailable("the project is not open in KiCad")

    specs = registry.all_specs()
    spec = next(s for s in specs if s.name == "research_part")
    backend: sdk_backend.SdkStreamingBackend

    def factory(options: ClaudeAgentOptions) -> ScriptedClient:
        handler = build_sdk_tool_list(
            [spec], backend.tool_ctx_factory, backend.kicadless_ctx_factory
        )[0].handler
        return ScriptedClient(options, handler, ARGS)

    backend = sdk_backend.SdkStreamingBackend(
        settings=settings,
        specs=specs,
        ctx_factory=no_kicad,
        db=db,
        client_factory=factory,
        research_query_fn=research_ok,
    )
    events = [ev async for ev in backend.start(project, one_item(), None)]
    final = next(e for e in events if isinstance(e, ToolResultEvt) and e.id == "toolu_rp")
    assert not final.is_error and final.content == "Summary: 2.5 MHz."


async def test_kicad_tools_still_report_kicad_unavailable(settings, project) -> None:  # type: ignore[no-untyped-def]
    from copperchat.kicad_session import KicadUnavailable

    def no_kicad() -> ToolContext:
        raise KicadUnavailable("the project is not open in KiCad")

    spec = next(s for s in registry.all_specs() if s.name == "run_erc")
    kicadless = lambda: ToolContext(project=project, session=None, settings=settings)  # type: ignore[arg-type]  # noqa: E731
    handler = build_sdk_tool_list([spec], no_kicad, kicadless)[0].handler
    res = await handler({})
    assert res.get("is_error") and "KiCad is not available" in res["content"][0]["text"]
