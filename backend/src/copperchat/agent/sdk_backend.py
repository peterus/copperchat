"""Claude Agent SDK adapter: options, guard, SDK message -> AgentEvent mapping, backend.

Measured SDK behaviour (claude-agent-sdk 0.2.168) is recorded in
``docs/spikes/1a-sdk-streaming.md``; the comments below cite its sections.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
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

from copperchat.agent.attachments import build_user_content
from copperchat.agent.events import (
    AgentEvent,
    QueuedItem,
    RateLimited,
    SessionId,
    TextDelta,
    ThinkingDelta,
    ToolResultEvt,
    ToolUse,
    TurnFinished,
    Usage,
)
from copperchat.agent.guards import DELEGATION_TOOL, ToolCallObserver, make_main_guard
from copperchat.agent.sdk_messages import (
    Totals,
    model_usage_totals,
    subagent_events,
    tool_result_content,
)
from copperchat.agent.sdk_tools import SERVER_NAME, build_sdk_tools, sdk_tool_name
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.projects import Project
from copperchat.research.runner import QueryFn, ResearchRunner
from copperchat.skills.assembly import SkillBundle, apply_skills, assemble_skills
from copperchat.tools import ToolContext, ToolSpec

log = logging.getLogger(__name__)

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
    skills: SkillBundle | None = None,
    on_tool_allowed: ToolCallObserver | None = None,
) -> ClaudeAgentOptions:
    """Main-agent options. Built-in tools: none, or exactly ``["Skill"]`` with enabled
    skills (Phase 8); the part-researcher is our ``research_part`` MCP tool, never a
    built-in. ``on_tool_allowed`` observes every call the guard allows."""
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
    options = ClaudeAgentOptions(
        tools=[],  # no built-in file/shell/web tools; init list = mcp__copperchat__* only
        allowed_tools=[],
        mcp_servers={SERVER_NAME: mcp_server},
        system_prompt=SYSTEM_PROMPT.format(project_name=project.name),
        permission_mode="default",
        can_use_tool=make_main_guard(guarded, on_allowed=on_tool_allowed),
        resume=resume_session_id,
        fork_session=fork_session,
        # The session store path is derived from cwd; it must stay stable per project
        # for resume to find the session file (spike 1A (c)).
        cwd=str(project.dir),
        env=env,
        include_partial_messages=True,
        setting_sources=[],
    )
    if skills is not None:
        options = apply_skills(options, skills)
    return options


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
        self._prev_totals: Totals | None = None
        # False while the token baseline in _prev_totals is unknown: after a result
        # without model_usage, the cumulative token totals were never seen.
        self._token_baseline_known = False
        self._interrupt_requested = False

    def note_interrupt_requested(self) -> None:
        """Our own interrupt() was sent; the next aborted result is an interrupt."""
        self._interrupt_requested = True

    def clear_interrupt_requested(self) -> None:
        """The interrupt request failed: an aborted result is a real error again."""
        self._interrupt_requested = False

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
            self._token_baseline_known = False
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
        out.extend(subagent_events(msg, parent))
        if isinstance(msg, AssistantMessage):
            self._add_subagent_usage(msg)

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
                text, images = tool_result_content(block.content)
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
        cum = model_usage_totals(msg)
        u = msg.usage or {}
        per_turn = Totals(
            int(u.get("input_tokens", 0) or 0),
            int(u.get("output_tokens", 0) or 0),
            int(u.get("cache_read_input_tokens", 0) or 0),
            int(u.get("cache_creation_input_tokens", 0) or 0),
        )
        if cum is not None and prev is not None and not self._token_baseline_known:
            # First model_usage after a fallback: its cumulative tokens include turns
            # never measured that way, so it only becomes the baseline; this turn's
            # tokens come from the per-turn usage.
            turn = per_turn
            self._token_baseline_known = True
        elif cum is None:
            # No model_usage: tokens from the per-turn (main-agent only) usage; the
            # token baseline is carried over unchanged, cost is still a delta.
            turn = per_turn
            base = prev or Totals()
            cum = Totals(
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
            base = prev or Totals()
            self._token_baseline_known = True
            turn = Totals(
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


async def user_prompt_with_attachments(
    item: QueuedItem, db: Database | None, project: Project
) -> dict[str, Any]:
    """``user_prompt`` with uploaded attachments as content blocks (Phase 8)."""
    prompt = user_prompt(item)
    if item.attachments and db is not None:
        prompt["message"]["content"] = await build_user_content(
            db, project, item.text, list(item.attachments)
        )
    return prompt


async def _prompts(
    items: AsyncIterator[QueuedItem], db: Database | None = None, project: Project | None = None
) -> AsyncIterator[dict[str, Any]]:
    # No buffering: the engine gates `items` (next item only after the previous
    # ResultMessage, spike 1A (a3)); we pull exactly when the SDK asks.
    async for item in items:
        if project is None:
            yield user_prompt(item)
        else:
            yield await user_prompt_with_attachments(item, db, project)


@dataclass(frozen=True)
class _StreamEnd:
    error: BaseException | None = None


class SdkStreamingBackend:
    """One long-lived ClaudeSDKClient per burst, fed by the engine's gated input.

    Phase 8: each burst assembles the project's skills (with ``db``), and runs
    ``research_part`` calls through a :class:`ResearchRunner` whose forwarded events are
    merged into this backend's event stream (a sub-agent row is never yielded before the
    ``research_part`` ToolUse it belongs to); the research usage is added to the turn's
    Usage; ``interrupt()`` also cancels running research queries.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        specs: Sequence[ToolSpec],
        ctx_factory: Callable[[], ToolContext],
        db: Database | None = None,
        client_factory: ClientFactory = ClaudeSDKClient,
        research_query_fn: QueryFn | None = None,
    ) -> None:
        self._settings = settings
        self._specs = list(specs)
        self._ctx_factory = ctx_factory
        self._db = db
        self._client_factory = client_factory
        self._research_query_fn = research_query_fn
        self._client: Any = None
        self._mapper: SdkEventMapper | None = None
        self._research: ResearchRunner | None = None

    async def start(
        self,
        project: Project,
        input_stream: AsyncIterator[QueuedItem],
        resume_session_id: str | None,
    ) -> AsyncIterator[AgentEvent]:
        skills = await assemble_skills(self._settings, project, self._db) if self._db else None
        queue: asyncio.Queue[AgentEvent | _StreamEnd] = asyncio.Queue()
        research = ResearchRunner(
            project,
            self._settings,
            emit=queue.put_nowait,
            notes_skill=skills.researcher_skill if skills else None,
            query_fn=self._research_query_fn,
        )

        def on_allowed(tool: str, inp: dict[str, Any], ctx: ToolPermissionContext) -> None:
            if tool == DELEGATION_TOOL:
                research.note_call(inp, ctx.tool_use_id)

        def ctx_factory() -> ToolContext:
            return replace(self._ctx_factory(), research=research)

        options = build_options(
            settings=self._settings,
            project=project,
            mcp_server=build_sdk_tools(self._specs, ctx_factory),
            tool_names=[s.name for s in self._specs],
            resume_session_id=resume_session_id,
            # Fork only at burst start, and only from a stored session (spike 1A (c)).
            fork_session=resume_session_id is not None,
            skills=skills,
            on_tool_allowed=on_allowed,
        )
        client = self._client_factory(options)
        mapper = SdkEventMapper()
        await client.connect(prompt=_prompts(input_stream, self._db, project))
        self._client, self._mapper, self._research = client, mapper, research

        async def pump() -> None:
            try:
                async for msg in client.receive_messages():
                    for ev in mapper.map(msg):
                        queue.put_nowait(ev)
            except Exception as exc:
                queue.put_nowait(_StreamEnd(exc))
            else:
                queue.put_nowait(_StreamEnd())

        pump_task = asyncio.create_task(pump())
        seen: set[str] = set()
        held: dict[str, list[AgentEvent]] = {}
        try:
            while True:
                item = await queue.get()
                if isinstance(item, _StreamEnd):
                    if item.error is not None:
                        raise item.error
                    return
                parent = getattr(item, "parent_tool_use_id", None)
                if parent and parent not in seen:
                    held.setdefault(parent, []).append(item)
                    continue
                if isinstance(item, TurnFinished) and held:
                    for evs in held.values():
                        for ev in evs:
                            yield ev
                    held.clear()
                if isinstance(item, Usage):
                    extra = research.take_usage()
                    if extra is not None:
                        item = _add_usage(item, extra)
                yield item
                if isinstance(item, ToolUse) and item.parent_tool_use_id is None:
                    seen.add(item.id)
                    for ev in held.pop(item.id, []):
                        yield ev
        finally:
            research.cancel_all()
            pump_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await pump_task
            self._client, self._mapper, self._research = None, None, None
            await client.disconnect()

    async def interrupt(self) -> None:
        client, mapper, research = self._client, self._mapper, self._research
        if research is not None:
            research.cancel_all()
        if client is None:
            return
        if mapper is not None:
            mapper.note_interrupt_requested()
        try:
            await client.interrupt()
        except Exception:
            # Not interrupted: the next error_during_execution is a real error.
            if mapper is not None:
                mapper.clear_interrupt_requested()
            raise


def _add_usage(usage: Usage, extra: Totals) -> Usage:
    """The turn's Usage plus the research queries it ran (owner decision 8)."""
    costs = [c for c in (usage.cost_usd, extra.cost_usd) if c is not None]
    return replace(
        usage,
        input_tokens=usage.input_tokens + extra.input_tokens,
        output_tokens=usage.output_tokens + extra.output_tokens,
        cache_read_tokens=usage.cache_read_tokens + extra.cache_read_tokens,
        cache_creation_tokens=usage.cache_creation_tokens + extra.cache_creation_tokens,
        cost_usd=sum(costs) if costs else None,
    )


# The backend chosen by spike 1A ("partial: gated generator", X20). On a no-go this
# would be SdkPerTurnBackend (Task 5.4b, not implemented).
SdkAgentBackend = SdkStreamingBackend
