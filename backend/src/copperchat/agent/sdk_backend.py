"""Claude Agent SDK adapter: options, guard, SDK message -> AgentEvent mapping, backend.

Measured SDK behaviour (claude-agent-sdk 0.2.168) is recorded in
``docs/spikes/1a-sdk-streaming.md``; the comments below cite its sections.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ConversationResetMessage,
    McpSdkServerConfig,
    PermissionResult,
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
from claude_agent_sdk.types import CanUseTool

from copperchat.agent.events import (
    AgentEvent,
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
from copperchat.agent.sdk_tools import SERVER_NAME, build_sdk_tools, sdk_tool_name
from copperchat.config import Settings
from copperchat.projects import Project
from copperchat.tools import ImageBlock, ToolContext, ToolSpec

SYSTEM_PROMPT = """You are CopperChat, an assistant for KiCad schematic and PCB design, running \
inside the self-hosted CopperChat web app (Powered by Claude).

You work on exactly one KiCad project: "{project_name}".
- You have no file, shell or web access. Everything you know about the design comes from your \
CopperChat tools, which query the running KiCad API server.
- Never guess design data: call a tool, then answer from its result. Report UUIDs in full and \
coordinates in millimetres.
- This release is read-only: you cannot edit schematics or boards. If asked to change the \
design, explain what you would change and that editing is not available yet.
- Use render_view to look at the design when spatial reasoning matters, and run_erc / run_drc \
to check it.
- Git: call git_commit only when the user explicitly asked for a commit in their current \
message. Never push.
- Lines starting with "[CopperChat notice]" come from the application, not from the user."""

# terminal_reason values of a turn cancelled by ClaudeSDKClient.interrupt() (spike 1A (b)).
_ABORTED = frozenset({"aborted_streaming", "aborted_tools"})
# Used when a rejected RateLimitEvent carries no reset time.
_RATE_LIMIT_FALLBACK = timedelta(minutes=5)

ClientFactory = Callable[[ClaudeAgentOptions], Any]


def make_tool_guard(allowed: frozenset[str]) -> CanUseTool:
    """``can_use_tool`` callback: allow exactly the registered CopperChat tools.

    The tools must NOT also be listed in ``allowed_tools`` - entries there bypass
    this callback (spike 1A (g4)).
    """

    async def guard(
        tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResult:
        if tool_name in allowed:
            return PermissionResultAllow(updated_input=tool_input)
        return PermissionResultDeny(
            message=f"Tool '{tool_name}' is not available in CopperChat.", interrupt=False
        )

    return guard


def build_options(
    *,
    settings: Settings,
    project: Project,
    mcp_server: McpSdkServerConfig,
    tool_names: Sequence[str],
    resume_session_id: str | None,
    fork_session: bool,
) -> ClaudeAgentOptions:
    guarded = frozenset(sdk_tool_name(n) for n in tool_names)
    env = {
        "CLAUDE_CONFIG_DIR": str(settings.data_dir / "claude"),
        # Mitigation against backgrounded sub-agents ending a turn early (spike 1A (h), X20 (3)).
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    }
    if settings.claude_code_oauth_token is not None:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token.get_secret_value()
    if settings.anthropic_api_key is not None:
        env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key.get_secret_value()
    return ClaudeAgentOptions(
        tools=[],  # no built-in file/shell/web tools; init list = mcp__copperchat__* only
        allowed_tools=[],
        mcp_servers={SERVER_NAME: mcp_server},
        system_prompt=SYSTEM_PROMPT.format(project_name=project.name),
        permission_mode="default",
        can_use_tool=make_tool_guard(guarded),
        resume=resume_session_id,
        fork_session=fork_session,
        # The session store path is derived from cwd; it must stay stable per project
        # for resume to find the session file (spike 1A (c)).
        cwd=str(project.dir),
        env=env,
        include_partial_messages=True,
        setting_sources=[],
    )


def _tool_result_content(
    content: str | list[dict[str, Any]] | None,
) -> tuple[str, tuple[ImageBlock, ...]]:
    if content is None:
        return "", ()
    if isinstance(content, str):
        return content, ()
    texts: list[str] = []
    images: list[ImageBlock] = []
    for item in content:
        if item.get("type") == "text":
            texts.append(str(item.get("text", "")))
        elif item.get("type") == "image":
            src = item.get("source", {})
            if src.get("type") == "base64":
                images.append(
                    ImageBlock(
                        mime=str(src.get("media_type", "image/png")),
                        data=base64.b64decode(src.get("data", "")),
                    )
                )
    return "\n".join(texts), tuple(images)


@dataclass(frozen=True)
class _Totals:
    """Cumulative per-client totals reported on a ResultMessage."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float | None = None

    def is_below(self, other: _Totals) -> bool:
        return (
            self.input_tokens < other.input_tokens
            or self.output_tokens < other.output_tokens
            or self.cache_read_tokens < other.cache_read_tokens
            or self.cache_creation_tokens < other.cache_creation_tokens
            or (
                self.cost_usd is not None
                and other.cost_usd is not None
                and self.cost_usd < other.cost_usd
            )
        )


def _model_usage_totals(msg: ResultMessage) -> _Totals | None:
    if not msg.model_usage:
        return None
    totals = [0, 0, 0, 0]
    keys = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
    for per_model in msg.model_usage.values():
        raw: dict[str, Any] = dict(per_model)
        for i, key in enumerate(keys):
            totals[i] += int(raw.get(key, 0) or 0)
    inp, out, cache_read, cache_creation = totals
    return _Totals(inp, out, cache_read, cache_creation, msg.total_cost_usd)


def _rate_limit_resets(info: RateLimitInfo | None) -> datetime | None:
    if info is None or not info.resets_at:
        return None
    return datetime.fromtimestamp(info.resets_at, UTC)


def _is_unsolicited(msg: ResultMessage) -> bool:
    """A result whose turn was not triggered by our own prompt (spike 1A (h), X20 (3)).

    Our prompts carry no origin (or ``{"kind": "human"}``); anything else, notably
    ``task-notification`` from a backgrounded delegation, is an injected turn.
    """
    origin = msg.origin
    return origin is not None and origin.get("kind") != "human"


class SdkEventMapper:
    """Stateful mapping of SDK messages to AgentEvents (one mapper per SDK client).

    Besides streaming state it keeps the client's previous cumulative usage totals:
    ``ResultMessage.model_usage`` and ``total_cost_usd`` are cumulative per client and
    include sub-agents, so a turn's Usage is the delta to the previous ResultMessage
    (spike 1A (f), X20 (5)).
    """

    def __init__(self) -> None:
        self._session_id: str | None = None
        self._current_stream_msg: str | None = None
        self._streamed_msgs: set[str] = set()
        self._partial_since_last_assistant = False
        self._rate: RateLimitInfo | None = None
        self._prev_totals: _Totals | None = None
        self._interrupt_requested = False

    def note_interrupt_requested(self) -> None:
        """Our own interrupt() was sent; the next aborted result is an interrupt."""
        self._interrupt_requested = True

    def map(self, msg: object) -> list[AgentEvent]:
        out: list[AgentEvent] = []
        sid = getattr(msg, "session_id", None)
        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            sid = msg.data.get("session_id")
        if isinstance(sid, str) and sid and sid != self._session_id:
            self._session_id = sid
            out.append(SessionId(sid))

        parent = getattr(msg, "parent_tool_use_id", None)
        if parent:
            # Sub-agent message (spike 1A (h4c)): only tagged, complete events; it never
            # touches the main agent's streaming state.
            self._map_subagent(msg, str(parent), out)
            return out
        if isinstance(msg, StreamEvent):
            self._map_stream(msg, out)
        elif isinstance(msg, AssistantMessage):
            self._map_assistant(msg, out)
        elif isinstance(msg, UserMessage):
            self._map_user(msg, out)
        elif isinstance(msg, RateLimitEvent):
            self._rate = msg.rate_limit_info
            # "allowed"/"allowed_warning" events arrive in normal operation (spike 1A (e)).
            if msg.rate_limit_info.status == "rejected":
                resets = _rate_limit_resets(msg.rate_limit_info)
                out.append(
                    RateLimited(resets_at=resets or datetime.now(UTC) + _RATE_LIMIT_FALLBACK)
                )
        elif isinstance(msg, ConversationResetMessage):
            # The CLI zeroes its running totals; the next result is absolute again.
            self._prev_totals = None
        elif isinstance(msg, ResultMessage):
            if _is_unsolicited(msg):
                # Leave the usage baseline and a pending interrupt request untouched: the
                # next solicited result's delta then includes this turn's usage.
                out.append(
                    TurnFinished(reason="error" if msg.is_error else "completed", unsolicited=True)
                )
            else:
                out.append(self._usage(msg))
                out.append(self._finished(msg))
        # Other SystemMessages (task_started, ...) carry nothing for the chat.
        return out

    def _map_subagent(self, msg: object, parent: str, out: list[AgentEvent]) -> None:
        """Map a message of an SDK sub-agent (``parent`` = the main agent's Agent call id).

        Sub-agent StreamEvents are ignored (none arrive, X20 (7)); text arrives as whole
        TextBlocks (with ``forward_subagent_text``); thinking, the echoed delegation prompt
        (string content) and tool-result images are dropped.
        """
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    out.append(
                        ToolUse(
                            id=block.id,
                            name=block.name,
                            input=dict(block.input),
                            parent_tool_use_id=parent,
                        )
                    )
                elif isinstance(block, TextBlock) and block.text.strip():
                    out.append(SubagentText(parent_tool_use_id=parent, text=block.text))
            self._add_subagent_usage(msg)
        elif isinstance(msg, UserMessage) and not isinstance(msg.content, str):
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    text, _images = _tool_result_content(block.content)
                    out.append(
                        ToolResultEvt(
                            id=block.tool_use_id,
                            content=text,
                            is_error=bool(block.is_error),
                            parent_tool_use_id=parent,
                        )
                    )

    def _add_subagent_usage(self, msg: AssistantMessage) -> None:
        """Deliberately a no-op (X20 (5), spike 1A h5).

        ``ResultMessage.usage`` excludes sub-agent tokens, but the turn's Usage is computed
        from the per-client cumulative ``model_usage`` / ``total_cost_usd`` delta, which
        already includes them. Summing sub-agent ``AssistantMessage.usage`` here would
        double-count and is unreliable anyway (its output tokens are streaming snapshots).
        """
        return None

    def _map_stream(self, msg: StreamEvent, out: list[AgentEvent]) -> None:
        ev = msg.event
        if ev.get("type") == "message_start":
            self._current_stream_msg = ev.get("message", {}).get("id")
        elif ev.get("type") == "content_block_delta":
            delta = ev.get("delta", {})
            kind = delta.get("type")
            if kind in ("text_delta", "thinking_delta"):
                self._partial_since_last_assistant = True
                if self._current_stream_msg:
                    self._streamed_msgs.add(self._current_stream_msg)
                if kind == "text_delta":
                    out.append(TextDelta(str(delta.get("text", ""))))
                else:
                    out.append(ThinkingDelta(str(delta.get("thinking", ""))))

    def _map_assistant(self, msg: AssistantMessage, out: list[AgentEvent]) -> None:
        if msg.message_id:
            streamed = msg.message_id in self._streamed_msgs
        else:
            streamed = self._partial_since_last_assistant
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                out.append(ToolUse(id=block.id, name=block.name, input=dict(block.input)))
            elif isinstance(block, TextBlock) and not streamed:
                out.append(TextDelta(block.text))
            elif isinstance(block, ThinkingBlock) and not streamed:
                out.append(ThinkingDelta(block.thinking))
        self._partial_since_last_assistant = False

    def _map_user(self, msg: UserMessage, out: list[AgentEvent]) -> None:
        if isinstance(msg.content, str):
            return
        for block in msg.content:
            # A backgrounded delegation ("Async agent launched ...") is an ordinary
            # tool_result here (X20 (3)).
            if isinstance(block, ToolResultBlock):
                text, images = _tool_result_content(block.content)
                out.append(
                    ToolResultEvt(
                        id=block.tool_use_id,
                        content=text,
                        images=images,
                        is_error=bool(block.is_error),
                    )
                )

    def _usage(self, msg: ResultMessage) -> Usage:
        prev = self._prev_totals
        cum = _model_usage_totals(msg)
        if cum is None:
            # No model_usage: tokens from the per-turn (main-agent only) usage; the
            # token baseline is carried over unchanged, cost is still a delta.
            u = msg.usage or {}
            turn = _Totals(
                int(u.get("input_tokens", 0) or 0),
                int(u.get("output_tokens", 0) or 0),
                int(u.get("cache_read_input_tokens", 0) or 0),
                int(u.get("cache_creation_input_tokens", 0) or 0),
            )
            base = prev or _Totals()
            cum = _Totals(
                base.input_tokens,
                base.output_tokens,
                base.cache_read_tokens,
                base.cache_creation_tokens,
                msg.total_cost_usd,
            )
            if prev is not None and cum.is_below(prev):
                prev = None
        else:
            if prev is not None and cum.is_below(prev):
                prev = None  # totals restarted (new conversation): this result is absolute
            base = prev or _Totals()
            turn = _Totals(
                cum.input_tokens - base.input_tokens,
                cum.output_tokens - base.output_tokens,
                cum.cache_read_tokens - base.cache_read_tokens,
                cum.cache_creation_tokens - base.cache_creation_tokens,
            )
        cost = cum.cost_usd
        if cost is not None and prev is not None and prev.cost_usd is not None:
            cost -= prev.cost_usd
        self._prev_totals = cum
        rate = self._rate
        return Usage(
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
            cache_read_tokens=turn.cache_read_tokens,
            cache_creation_tokens=turn.cache_creation_tokens,
            cost_usd=cost,
            rate_limit_status=rate.status if rate else None,
            rate_limit_resets_at=_rate_limit_resets(rate),
        )

    def _finished(self, msg: ResultMessage) -> TurnFinished:
        requested, self._interrupt_requested = self._interrupt_requested, False
        if msg.terminal_reason in _ABORTED or (
            requested and msg.subtype == "error_during_execution"
        ):
            return TurnFinished(reason="interrupted")
        if msg.is_error:
            detail = "; ".join(msg.errors or []) or msg.result or msg.subtype
            return TurnFinished(reason="error", error=detail)
        return TurnFinished(reason="completed")


def user_prompt(item: QueuedItem) -> dict[str, Any]:
    """Streaming-input user message, in the shape used by spike 1A."""
    return {
        "type": "user",
        "message": {"role": "user", "content": item.text},
        "parent_tool_use_id": None,
    }


async def _prompts(items: AsyncIterator[QueuedItem]) -> AsyncIterator[dict[str, Any]]:
    # No buffering: the engine gates `items` (next item only after the previous
    # ResultMessage, spike 1A (a3)); we pull exactly when the SDK asks.
    async for item in items:
        yield user_prompt(item)


class SdkStreamingBackend:
    """One long-lived ClaudeSDKClient per burst, fed by the engine's gated input."""

    def __init__(
        self,
        *,
        settings: Settings,
        specs: Sequence[ToolSpec],
        ctx_factory: Callable[[], ToolContext],
        client_factory: ClientFactory = ClaudeSDKClient,
    ) -> None:
        self._settings = settings
        self._specs = list(specs)
        self._ctx_factory = ctx_factory
        self._client_factory = client_factory
        self._client: Any = None
        self._mapper: SdkEventMapper | None = None

    async def start(
        self,
        project: Project,
        input_stream: AsyncIterator[QueuedItem],
        resume_session_id: str | None,
    ) -> AsyncIterator[AgentEvent]:
        options = build_options(
            settings=self._settings,
            project=project,
            mcp_server=build_sdk_tools(self._specs, self._ctx_factory),
            tool_names=[s.name for s in self._specs],
            resume_session_id=resume_session_id,
            # Fork only at burst start, and only from a stored session (spike 1A (c)).
            fork_session=resume_session_id is not None,
        )
        client = self._client_factory(options)
        mapper = SdkEventMapper()
        await client.connect(prompt=_prompts(input_stream))
        self._client, self._mapper = client, mapper
        try:
            async for msg in client.receive_messages():
                for ev in mapper.map(msg):
                    yield ev
        finally:
            self._client, self._mapper = None, None
            await client.disconnect()

    async def interrupt(self) -> None:
        client, mapper = self._client, self._mapper
        if client is None:
            return
        if mapper is not None:
            mapper.note_interrupt_requested()
        await client.interrupt()
