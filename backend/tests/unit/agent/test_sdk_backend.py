import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ConversationResetMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from pydantic import SecretStr

from copperchat.agent.events import (
    QueuedItem,
    RateLimited,
    SessionId,
    SubagentText,
    TextDelta,
    ThinkingDelta,
    ToolResultEvt,
    ToolUse,
    TurnFinished,
    Usage,
)
from copperchat.agent.sdk_backend import (
    SdkEventMapper,
    SdkStreamingBackend,
    build_options,
    make_tool_guard,
    system_prompt,
    user_prompt,
)
from copperchat.config import Settings
from copperchat.projects import Project
from tests.support.conversations import conversation_row

MCP = {"type": "sdk", "name": "copperchat", "instance": None}


@pytest.fixture
def project(tmp_path: Path) -> Project:
    d = tmp_path / "demo"
    d.mkdir()
    return Project(
        id="a1b2c3d4e5f6",
        name="demo",
        rel_path="demo/demo.kicad_pro",
        dir=d,
        pro_file=d / "demo.kicad_pro",
        sch_file=None,
        pcb_file=None,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        projects_root=tmp_path,
        data_dir=tmp_path / "data",
        claude_code_oauth_token=SecretStr("tok-123"),
    )


# --- options and guard -----------------------------------------------------------------


def test_options_disable_builtins_and_guard_only_our_tools(settings, project):
    opts = build_options(
        settings=settings,
        project=project,
        mcp_server=MCP,  # type: ignore[arg-type]
        tool_names=["project_info", "run_erc"],
        resume_session_id=None,
        fork_session=False,
    )
    assert opts.tools == []
    # Spike 1A (g4): entries in allowed_tools bypass can_use_tool, so none are listed.
    assert opts.allowed_tools == []
    assert set(opts.mcp_servers) == {"copperchat"}  # type: ignore[arg-type]
    assert isinstance(opts.system_prompt, str)
    assert "CopperChat" in opts.system_prompt and "Claude Code" not in opts.system_prompt
    assert '"demo"' in opts.system_prompt
    assert opts.include_partial_messages is True
    assert opts.env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-123"
    assert "ANTHROPIC_API_KEY" not in opts.env
    assert opts.env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    assert opts.env["CLAUDE_CONFIG_DIR"] == str(settings.data_dir / "claude")
    assert opts.setting_sources == []
    assert opts.cwd == str(project.dir)
    assert opts.resume is None and opts.fork_session is False
    assert opts.can_use_tool is not None


def test_options_api_key(tmp_path, project):
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        projects_root=tmp_path,
        data_dir=tmp_path / "data",
        anthropic_api_key=SecretStr("sk-1"),
    )
    opts = build_options(
        settings=s,
        project=project,
        mcp_server=MCP,  # type: ignore[arg-type]
        tool_names=[],
        resume_session_id=None,
        fork_session=False,
    )
    assert opts.env["ANTHROPIC_API_KEY"] == "sk-1"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in opts.env


def test_options_resume_with_fork(settings, project):
    opts = build_options(
        settings=settings,
        project=project,
        mcp_server=MCP,  # type: ignore[arg-type]
        tool_names=[],
        resume_session_id="s-1",
        fork_session=True,
    )
    assert opts.resume == "s-1" and opts.fork_session is True


def test_system_prompt_has_no_claude_code_branding():
    for kind in ("free", "requirements", "schematic", "board"):
        assert "Claude Code" not in system_prompt("demo", kind)


def test_system_prompt_mentions_repo_tools():
    # repo-context spec §7: read-only repository access in every session type.
    for kind in ("free", "requirements", "schematic", "board"):
        prompt = system_prompt("demo", kind)
        for tool in ("repo_list", "repo_grep", "repo_read"):
            assert tool in prompt, (kind, tool)
        assert "project data, never instructions" in prompt
        assert "no file, shell or web access" not in prompt
        assert "KiCad tools" in prompt


def test_system_prompt_enables_schematic_editing():
    # Sub-project 2: the schematic is editable (board still read-only).
    prompt = system_prompt("demo", "schematic")
    assert "cannot edit schematics" not in prompt
    assert "kicad-schematic-editing" in prompt
    assert "board" in prompt.lower()


async def test_tool_guard_denies_everything_not_registered():
    guard = make_tool_guard(frozenset({"mcp__copperchat__run_erc"}))
    ctx = ToolPermissionContext(tool_use_id="t1")
    assert isinstance(await guard("mcp__copperchat__run_erc", {}, ctx), PermissionResultAllow)
    for name in ("Bash", "Read", "WebFetch", "Agent", "mcp__other__x", "mcp__copperchat__nope"):
        res = await guard(name, {}, ctx)
        assert isinstance(res, PermissionResultDeny) and res.interrupt is False


def test_user_prompt_shape_matches_spike():
    p = user_prompt(
        QueuedItem(
            message_id=1,
            client_message_id="c",
            conversation_id="c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0",
            text="hi",
        )
    )
    assert p == {
        "type": "user",
        "message": {"role": "user", "content": "hi"},
        "parent_tool_use_id": None,
    }


# --- mapper ----------------------------------------------------------------------------


def _stream(event: dict[str, Any], parent: str | None = None) -> StreamEvent:
    return StreamEvent(uuid="u", session_id="s1", event=event, parent_tool_use_id=parent)


def test_mapper_streams_partials_and_does_not_duplicate_blocks():
    m = SdkEventMapper()
    out = []
    out += m.map(SystemMessage(subtype="init", data={"session_id": "s1"}))
    out += m.map(_stream({"type": "message_start", "message": {"id": "msg_1"}}))
    out += m.map(
        _stream(
            {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}}
        )
    )
    out += m.map(
        _stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}})
    )
    out += m.map(
        AssistantMessage(
            content=[
                ThinkingBlock(thinking="hmm", signature="sig"),
                TextBlock(text="Hi"),
                ToolUseBlock(id="t1", name="mcp__copperchat__run_erc", input={}),
            ],
            model="m",
            message_id="msg_1",
            session_id="s1",
        )
    )
    assert out == [
        SessionId("s1"),
        ThinkingDelta("hmm"),
        TextDelta("Hi"),
        ToolUse(id="t1", name="mcp__copperchat__run_erc", input={}),
    ]


def test_mapper_uses_blocks_when_no_partials_were_seen():
    m = SdkEventMapper()
    out = m.map(AssistantMessage(content=[TextBlock(text="Whole")], model="m", message_id="msg_2"))
    assert out == [TextDelta("Whole")]


def test_mapper_tool_result_with_image():
    m = SdkEventMapper()
    block = ToolResultBlock(
        tool_use_id="t1",
        is_error=False,
        content=[
            {"type": "text", "text": '{"ok": true}'},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "UE5H"},
            },
        ],
    )
    [ev] = m.map(UserMessage(content=[block]))
    assert isinstance(ev, ToolResultEvt)
    assert ev.id == "t1" and ev.content == '{"ok": true}' and ev.is_error is False
    assert ev.images[0].mime == "image/png" and ev.images[0].data == b"PNG"


def test_mapper_async_agent_launched_is_an_ordinary_tool_result():
    # X20 (3): a backgrounded delegation must not crash the mapper.
    m = SdkEventMapper()
    out = m.map(
        AssistantMessage(
            content=[ToolUseBlock(id="a1", name="Agent", input={"subagent_type": "x"})], model="m"
        )
    )
    out += m.map(
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="a1",
                    content=[{"type": "text", "text": "Async agent launched successfully."}],
                )
            ]
        )
    )
    out += m.map(SystemMessage(subtype="task_started", data={"task_id": "k"}))
    assert out == [
        ToolUse(id="a1", name="Agent", input={"subagent_type": "x"}),
        ToolResultEvt(id="a1", content="Async agent launched successfully."),
    ]


def test_mapper_maps_subagent_messages_with_parent_id():
    # Spike 1A (h4c): sub-agent messages carry parent_tool_use_id = the Agent call's id.
    m = SdkEventMapper()
    agent_input = {"subagent_type": "part-researcher", "description": "TPS62133", "prompt": "q"}
    out = []
    out += m.map(
        AssistantMessage(
            content=[ToolUseBlock(id="a1", name="Agent", input=agent_input)],
            model="m",
            message_id="msg_1",
        )
    )
    out += m.map(
        _stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "p"}}, "a1")
    )
    out += m.map(UserMessage(content="q", parent_tool_use_id="a1"))  # echoed delegation prompt
    out += m.map(
        AssistantMessage(
            content=[
                ThinkingBlock(thinking="hmm", signature="s"),
                TextBlock(text="Searching"),
                TextBlock(text="  "),
                ToolUseBlock(id="w1", name="WebSearch", input={"query": "TPS62133"}),
            ],
            model="m",
            message_id="msg_2",
            parent_tool_use_id="a1",
        )
    )
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": ""}}
    out += m.map(
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="w1",
                    content=[{"type": "text", "text": "hits"}, image],
                    is_error=False,
                )
            ],
            parent_tool_use_id="a1",
        )
    )
    out += m.map(
        UserMessage(content=[ToolResultBlock(tool_use_id="a1", content="summary", is_error=False)])
    )
    assert out == [
        ToolUse(id="a1", name="Agent", input=agent_input),
        SessionId("s1"),  # first message carrying a session id
        SubagentText(parent_tool_use_id="a1", text="Searching"),
        ToolUse(id="w1", name="WebSearch", input={"query": "TPS62133"}, parent_tool_use_id="a1"),
        ToolResultEvt(id="w1", content="hits", parent_tool_use_id="a1"),  # image dropped
        ToolResultEvt(id="a1", content="summary"),
    ]


def test_mapper_subagent_text_does_not_suppress_main_agent_blocks():
    # A sub-agent message between the main agent's partials and its AssistantMessage must
    # not reset the main agent's "already streamed" bookkeeping (no duplicate text).
    m = SdkEventMapper()
    out = m.map(_stream({"type": "message_start", "message": {"id": "msg_1"}}))
    out += m.map(
        _stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}})
    )
    out += m.map(
        AssistantMessage(
            content=[TextBlock(text="sub")], model="m", message_id="sub_1", parent_tool_use_id="a1"
        )
    )
    out += m.map(AssistantMessage(content=[TextBlock(text="Hi")], model="m", message_id="msg_1"))
    assert out == [
        SessionId("s1"),
        TextDelta("Hi"),
        SubagentText(parent_tool_use_id="a1", text="sub"),
    ]


def test_mapper_interrupt_during_subagent_is_interrupted():
    # Spike 1A (h5): interrupting while the sub-agent runs ends the whole turn.
    m = SdkEventMapper()
    m.map(
        AssistantMessage(
            content=[ToolUseBlock(id="a1", name="Agent", input={})], model="m", message_id="x"
        )
    )
    m.map(
        AssistantMessage(
            content=[TextBlock(text="sub")], model="m", message_id="y", parent_tool_use_id="a1"
        )
    )
    m.note_interrupt_requested()
    out = m.map(_result(subtype="error_during_execution", is_error=True))
    assert out[-1] == TurnFinished(reason="interrupted")


def _result(**kw: Any) -> ResultMessage:
    base: dict[str, Any] = dict(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s1",
        total_cost_usd=0.5,
        usage={
            "input_tokens": 10,
            "output_tokens": 3,
            "cache_read_input_tokens": 7,
            "cache_creation_input_tokens": 1,
        },
    )
    base.update(kw)
    return ResultMessage(**base)


def _mu(inp: int, out: int, cr: int, cc: int, cost: float) -> dict[str, Any]:
    return {
        "inputTokens": inp,
        "outputTokens": out,
        "cacheReadInputTokens": cr,
        "cacheCreationInputTokens": cc,
        "webSearchRequests": 0,
        "costUSD": cost,
        "contextWindow": 200000,
        "maxOutputTokens": 32000,
    }


def test_mapper_result_reasons_and_usage_fallback_without_model_usage():
    m = SdkEventMapper()
    m.map(SystemMessage(subtype="init", data={"session_id": "s1"}))
    usage, fin = m.map(_result())
    assert usage == Usage(
        input_tokens=10, output_tokens=3, cache_read_tokens=7, cache_creation_tokens=1, cost_usd=0.5
    )
    assert fin == TurnFinished(reason="completed")
    err = m.map(_result(is_error=True, errors=["boom"], total_cost_usd=0.6))[1]
    assert err == TurnFinished(reason="error", error="boom")


def test_mapper_usage_is_delta_of_cumulative_model_usage_and_cost():
    # Spike 1A (f)/(h5): model_usage and total_cost_usd are cumulative per client and
    # include sub-agents; the turn's usage is the delta against the previous result.
    m = SdkEventMapper()
    first = m.map(
        _result(
            total_cost_usd=0.0002619,
            model_usage={"claude-haiku-5-5": _mu(1176, 5, 0, 669, 0.0002619)},
        )
    )[-2]
    assert first == Usage(
        input_tokens=1176,
        output_tokens=5,
        cache_read_tokens=0,
        cache_creation_tokens=669,
        cost_usd=0.0002619,
    )
    second = m.map(
        _result(
            total_cost_usd=0.0004066,
            model_usage={
                "claude-haiku-5-5": _mu(1178, 10, 0, 1379, 0.0004),
                "claude-sonnet-5-5": _mu(4, 2, 100, 0, 0.0000066),
            },
        )
    )[-2]
    assert isinstance(second, Usage)
    assert (second.input_tokens, second.output_tokens) == (2 + 4, 5 + 2)
    assert (second.cache_read_tokens, second.cache_creation_tokens) == (100, 710)
    assert second.cost_usd == pytest.approx(0.0004066 - 0.0002619)


def test_mapper_usage_baseline_resets_after_conversation_reset():
    m = SdkEventMapper()
    m.map(_result(total_cost_usd=1.0, model_usage={"m": _mu(100, 10, 0, 0, 1.0)}))
    m.map(ConversationResetMessage(new_conversation_id="c2", uuid="u", session_id="s1"))
    usage = m.map(_result(total_cost_usd=0.2, model_usage={"m": _mu(20, 2, 0, 0, 0.2)}))[0]
    assert usage == Usage(
        input_tokens=20, output_tokens=2, cache_read_tokens=0, cache_creation_tokens=0, cost_usd=0.2
    )


def test_mapper_interrupt_results_map_to_interrupted():
    # Spike 1A (b): interrupt -> error_during_execution with aborted_* terminal reason.
    m = SdkEventMapper()
    for reason in ("aborted_streaming", "aborted_tools"):
        res = _result(subtype="error_during_execution", is_error=True, terminal_reason=reason)
        assert m.map(res)[-1] == TurnFinished(reason="interrupted")


def test_mapper_requested_interrupt_without_terminal_reason_is_interrupted():
    m = SdkEventMapper()
    m.note_interrupt_requested()
    res = _result(subtype="error_during_execution", is_error=True)
    assert m.map(res)[-1] == TurnFinished(reason="interrupted")
    # the flag is consumed by that result
    assert m.map(res)[-1] == TurnFinished(reason="error", error="error_during_execution")


def test_mapper_rate_limit_allowed_is_not_rate_limited_but_reaches_usage():
    m = SdkEventMapper()
    resets = int(datetime(2026, 10, 15, 17, 0, tzinfo=UTC).timestamp())
    info = RateLimitInfo(status="allowed", resets_at=resets, rate_limit_type="five_hour")
    assert m.map(RateLimitEvent(rate_limit_info=info, uuid="u", session_id="s1")) == [
        SessionId("s1")
    ]
    usage = m.map(_result())[0]
    assert isinstance(usage, Usage)
    assert usage.rate_limit_status == "allowed"
    assert usage.rate_limit_resets_at == datetime(2026, 10, 15, 17, 0, tzinfo=UTC)


def test_mapper_rate_limit_rejected_and_status_on_usage():
    m = SdkEventMapper()
    m.map(SystemMessage(subtype="init", data={"session_id": "s1"}))
    resets = int(datetime(2026, 10, 15, 17, 0, tzinfo=UTC).timestamp())
    [ev] = m.map(
        RateLimitEvent(
            rate_limit_info=RateLimitInfo(status="rejected", resets_at=resets),
            uuid="u",
            session_id="s1",
        )
    )
    assert ev == RateLimited(resets_at=datetime(2026, 10, 15, 17, 0, tzinfo=UTC))
    usage = m.map(_result())[0]
    assert isinstance(usage, Usage) and usage.rate_limit_status == "rejected"


def test_mapper_rate_limit_rejected_without_reset_time_uses_fallback():
    m = SdkEventMapper()
    m.map(SystemMessage(subtype="init", data={"session_id": "s1"}))
    before = datetime.now(UTC)
    [ev] = m.map(
        RateLimitEvent(rate_limit_info=RateLimitInfo(status="rejected"), uuid="u", session_id="s1")
    )
    assert isinstance(ev, RateLimited) and ev.resets_at > before


# --- streaming backend ------------------------------------------------------------------


class FakeSdkClient:
    """Mimics ClaudeSDKClient: consumes the prompt iterable, replies with scripted messages."""

    def __init__(self, options: Any, replies: list[list[Any]]) -> None:
        self.options = options
        self.replies = replies
        self.prompts: list[dict[str, Any]] = []
        self.out: asyncio.Queue[Any] = asyncio.Queue()
        self.interrupts = 0
        self.disconnected = False

    async def connect(self, prompt: Any) -> None:
        async def pump() -> None:
            async for p in prompt:
                self.prompts.append(p)
                for msg in self.replies.pop(0):
                    await self.out.put(msg)
            await self.out.put(None)

        self.task = asyncio.create_task(pump())

    async def receive_messages(self):  # type: ignore[no-untyped-def]
        while (msg := await self.out.get()) is not None:
            yield msg

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def disconnect(self) -> None:
        self.disconnected = True


async def test_streaming_backend_maps_messages_and_sends_user_prompts(settings, project):
    clients: list[FakeSdkClient] = []

    def factory(options: Any) -> FakeSdkClient:
        delta = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}}
        c = FakeSdkClient(
            options,
            [
                [_stream(delta), _result(total_cost_usd=0.5)],
                [_result(total_cost_usd=0.75)],
            ],
        )
        clients.append(c)
        return c

    backend = SdkStreamingBackend(
        settings=settings,
        specs=[],
        ctx_factory=lambda: None,  # type: ignore[arg-type,return-value]
        client_factory=factory,
    )

    async def items():  # type: ignore[no-untyped-def]
        yield QueuedItem(
            message_id=1,
            client_message_id="c1",
            conversation_id="c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0",
            text="hello",
        )
        yield QueuedItem(
            message_id=2,
            client_message_id="c2",
            conversation_id="c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0",
            text="again",
        )

    events = [ev async for ev in backend.start(project, conversation_row(), items(), "s-old")]
    assert TextDelta("ok") in events and events[-1] == TurnFinished(reason="completed")
    costs = [e.cost_usd for e in events if isinstance(e, Usage)]
    assert costs == [0.5, pytest.approx(0.25)]
    c = clients[0]
    assert [p["message"] for p in c.prompts] == [
        {"role": "user", "content": "hello"},
        {"role": "user", "content": "again"},
    ]
    assert c.options.resume == "s-old" and c.options.fork_session is True
    assert c.disconnected is True


async def test_streaming_backend_no_fork_without_stored_session(settings, project):
    clients: list[FakeSdkClient] = []

    def factory(options: Any) -> FakeSdkClient:
        c = FakeSdkClient(options, [[_result()]])
        clients.append(c)
        return c

    backend = SdkStreamingBackend(
        settings=settings,
        specs=[],
        ctx_factory=lambda: None,  # type: ignore[arg-type,return-value]
        client_factory=factory,
    )

    async def items():  # type: ignore[no-untyped-def]
        yield QueuedItem(
            message_id=1,
            client_message_id="c1",
            conversation_id="c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0",
            text="x",
        )

    _ = [ev async for ev in backend.start(project, conversation_row(), items(), None)]
    assert clients[0].options.resume is None and clients[0].options.fork_session is False


async def test_streaming_backend_interrupt_delegates_to_client(settings, project):
    gate = asyncio.Event()
    client_box: list[FakeSdkClient] = []

    def factory(options: Any) -> FakeSdkClient:
        aborted = _result(subtype="error_during_execution", is_error=True, terminal_reason=None)
        c = FakeSdkClient(options, [[aborted]])
        client_box.append(c)
        return c

    backend = SdkStreamingBackend(
        settings=settings,
        specs=[],
        ctx_factory=lambda: None,  # type: ignore[arg-type,return-value]
        client_factory=factory,
    )
    await backend.interrupt()  # no client yet: a no-op

    async def items():  # type: ignore[no-untyped-def]
        await gate.wait()
        yield QueuedItem(
            message_id=1,
            client_message_id="c1",
            conversation_id="c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0",
            text="x",
        )

    gen = backend.start(project, conversation_row(), items(), None)
    first = asyncio.ensure_future(gen.__anext__())
    # The burst resolves the repository policy (a git subprocess) before it connects.
    for _ in range(500):
        if client_box:
            break
        await asyncio.sleep(0.01)
    await backend.interrupt()
    assert client_box[0].interrupts == 1
    gate.set()
    rest = [await first] + [ev async for ev in gen]
    assert rest[-1] == TurnFinished(reason="interrupted")
    assert client_box[0].disconnected is True


async def test_streaming_backend_failed_interrupt_does_not_mislabel_next_error(settings, project):
    gate = asyncio.Event()
    client_box: list[FakeSdkClient] = []

    class BrokenInterruptClient(FakeSdkClient):
        async def interrupt(self) -> None:
            raise RuntimeError("control request failed")

    def factory(options: Any) -> FakeSdkClient:
        failed = _result(subtype="error_during_execution", is_error=True, errors=["real failure"])
        c = BrokenInterruptClient(options, [[failed]])
        client_box.append(c)
        return c

    backend = SdkStreamingBackend(
        settings=settings,
        specs=[],
        ctx_factory=lambda: None,  # type: ignore[arg-type,return-value]
        client_factory=factory,
    )

    async def items():  # type: ignore[no-untyped-def]
        await gate.wait()
        yield QueuedItem(
            message_id=1,
            client_message_id="c1",
            conversation_id="c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0",
            text="x",
        )

    gen = backend.start(project, conversation_row(), items(), None)
    first = asyncio.ensure_future(gen.__anext__())
    # The burst resolves the repository policy (a git subprocess) before it connects.
    for _ in range(500):
        if client_box:
            break
        await asyncio.sleep(0.01)
    with pytest.raises(RuntimeError):
        await backend.interrupt()
    gate.set()
    rest = [await first] + [ev async for ev in gen]
    assert rest[-1] == TurnFinished(reason="error", error="real failure")


def test_mapper_tool_result_with_invalid_base64_image_skips_the_image():
    m = SdkEventMapper()
    block = ToolResultBlock(
        tool_use_id="t1",
        is_error=False,
        content=[
            {"type": "text", "text": "ok"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A"}},
        ],
    )
    [ev] = m.map(UserMessage(content=[block]))
    assert isinstance(ev, ToolResultEvt)
    assert ev.content == "ok" and ev.images == ()


def test_mapper_first_model_usage_after_fallback_is_baseline_not_full_cumulative():
    m = SdkEventMapper()
    # first result of the client has no model_usage: per-turn usage fallback
    first = m.map(_result(total_cost_usd=0.5))[-2]
    assert isinstance(first, Usage) and first.input_tokens == 10
    # the next result carries cumulative model_usage for the whole client (incl. turn 1)
    second = m.map(_result(total_cost_usd=0.8, model_usage={"m": _mu(500, 50, 70, 10, 0.8)}))[-2]
    assert second == Usage(
        input_tokens=10,
        output_tokens=3,
        cache_read_tokens=7,
        cache_creation_tokens=1,
        cost_usd=pytest.approx(0.3),
    )
    third = m.map(_result(total_cost_usd=1.0, model_usage={"m": _mu(520, 55, 70, 12, 1.0)}))[-2]
    assert third == Usage(
        input_tokens=20,
        output_tokens=5,
        cache_read_tokens=0,
        cache_creation_tokens=2,
        cost_usd=pytest.approx(0.2),
    )


def test_mapper_task_notification_result_is_unsolicited_and_keeps_state():
    # X20 (3): a backgrounded delegation can make the CLI run an injected turn whose
    # ResultMessage is not the answer to any gated input item.
    m = SdkEventMapper()
    m.map(_result(total_cost_usd=1.0, model_usage={"m": _mu(100, 10, 0, 0, 1.0)}))
    m.note_interrupt_requested()
    stray = m.map(
        _result(
            total_cost_usd=1.5,
            model_usage={"m": _mu(150, 15, 0, 0, 1.5)},
            origin={"kind": "task-notification"},
        )
    )
    assert stray == [TurnFinished(reason="completed", unsolicited=True)]
    # Baseline untouched: the next real result's delta includes the stray turn's usage,
    # and the pending interrupt request was not consumed by the stray result.
    usage, fin = m.map(
        _result(
            subtype="error_during_execution",
            is_error=True,
            total_cost_usd=2.0,
            model_usage={"m": _mu(200, 20, 0, 0, 2.0)},
        )
    )
    assert isinstance(usage, Usage)
    assert (usage.input_tokens, usage.output_tokens) == (100, 10)
    assert usage.cost_usd == pytest.approx(1.0)
    assert fin == TurnFinished(reason="interrupted")


def test_mapper_human_origin_result_is_a_normal_turn_end():
    m = SdkEventMapper()
    assert m.map(_result(origin={"kind": "human"}))[-1] == TurnFinished(reason="completed")


async def test_streaming_backend_guards_repo_read_through_both_entry_points(settings, project):
    from copperchat.agent.sdk_tools import sdk_tool_name
    from copperchat.repo_context.specs import TOOL_SPECS as REPO_SPECS

    (project.dir / "README.md").write_text("# demo\n")
    (project.dir / ".env").write_text("TOKEN=x\n")
    clients: list[FakeSdkClient] = []

    def factory(options: Any) -> FakeSdkClient:
        c = FakeSdkClient(options, [[_result()]])
        clients.append(c)
        return c

    backend = SdkStreamingBackend(
        settings=settings,
        specs=REPO_SPECS,
        ctx_factory=lambda: None,  # type: ignore[arg-type,return-value]
        client_factory=factory,
    )

    async def items():  # type: ignore[no-untyped-def]
        yield QueuedItem(
            message_id=1,
            client_message_id="c1",
            conversation_id="c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0",
            text="x",
        )

    _ = [ev async for ev in backend.start(project, conversation_row(), items(), None)]
    options = clients[0].options
    tool = sdk_tool_name("repo_read")
    hook = options.hooks["PreToolUse"][0].hooks[0]

    async def via_hook(path: str) -> bool:
        out = await hook(
            {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"path": path}},
            "t",
            {},
        )
        return out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"

    async def via_can_use_tool(path: str) -> bool:
        res = await options.can_use_tool(tool, {"path": path}, ToolPermissionContext())
        return isinstance(res, PermissionResultAllow)

    for decide in (via_can_use_tool, via_hook):
        assert await decide("README.md"), decide
        assert not await decide(".env"), decide
