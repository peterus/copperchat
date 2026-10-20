# backend/src/copperchat/agent/fake.py
"""Scriptable in-process AgentBackend for tests and dev_fake_agent=True (no token needed)."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

from claude_agent_sdk import PermissionResultDeny, ToolPermissionContext

from copperchat.agent.events import (
    AgentEvent,
    QueuedItem,
    SessionId,
    SubagentText,
    TextDelta,
    ThinkingDelta,
    ToolResultEvt,
    ToolUse,
    TurnFinished,
    Usage,
)
from copperchat.agent.guards import make_main_guard
from copperchat.agent.profiles import bind_conversation, profile_specs
from copperchat.agent.sdk_tools import build_sdk_tool_list, mcp_result_to_event, sdk_tool_name
from copperchat.config import Settings
from copperchat.projects import Project
from copperchat.tools import ToolContext, ToolSpec

if TYPE_CHECKING:
    from copperchat.db.repos.conversations import ConversationRow
    from copperchat.kicad_session import KicadSession


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Think:
    text: str


@dataclass(frozen=True)
class Tool:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Wait:
    event: asyncio.Event


@dataclass(frozen=True)
class Emit:
    event: AgentEvent


@dataclass(frozen=True)
class Finish:
    reason: Literal["completed", "interrupted", "error"] = "completed"
    error: str | None = None


@dataclass(frozen=True)
class Delegate:
    """A main-agent ``Agent`` call run by an SDK sub-agent (spike 1A (h4c)).

    Emits the ``Agent`` tool_use, then the sub-agent's ``steps`` tagged with its id
    (``Text`` -> SubagentText; ``Tool`` -> tool_use + a canned "<name> ok" result, no
    handler is run; ``Wait``/``Emit`` as usual), then the Agent call's own tool_result
    carrying ``summary``.
    """

    description: str
    steps: tuple[Text | Tool | Wait | Emit, ...] = ()
    summary: str = ""
    subagent_type: str = "part-researcher"


Step = Text | Think | Tool | Wait | Emit | Finish | Delegate
ScriptFor = Callable[[QueuedItem], list[Step]]


def echo_script(item: QueuedItem) -> list[Step]:
    return [Text("You said: "), Text(item.text)]


@dataclass
class FakeBurst:
    """What one burst saw (spec §14.3): its conversation, its profile's tool list and the
    prompt text of every item (incl. the injected notices)."""

    conversation_id: str
    conversation_type: str
    tool_names: list[str]
    prompts: list[str] = field(default_factory=list)


class FakeAgent:
    def __init__(
        self,
        script_for: ScriptFor | None = None,
        *,
        specs: Sequence[ToolSpec] = (),
        ctx_factory: Callable[[], ToolContext] | None = None,
        ignore_interrupt: bool = False,
        settings: Settings | None = None,
        turn_context: Callable[[], ToolContext] | None = None,
    ) -> None:
        self._script_for = script_for or echo_script
        self._specs = list(specs)
        self._ctx_factory = ctx_factory
        # KiCad-less tools (requires_kicad=False) get their own context, like under the
        # SDK backend; it carries the running turn's run id (S-C3).
        self._settings = settings
        self._turn_context = turn_context
        self._project: Project | None = None
        self._ignore_interrupt = ignore_interrupt
        self._interrupted = asyncio.Event()
        self._tool_ids = itertools.count(1)
        self._burst = 0
        self.prompts: list[QueuedItem] = []
        self.resume_ids: list[str | None] = []
        self.conversations: list[str] = []  # the conversation of each burst
        self.bursts: list[FakeBurst] = []
        self.interrupts = 0
        self._conversation: ConversationRow | None = None
        self._burst_specs: list[ToolSpec] = self._specs

    async def interrupt(self) -> None:
        self.interrupts += 1
        if not self._ignore_interrupt:
            self._interrupted.set()

    async def start(
        self,
        project: Project,
        conversation: ConversationRow,
        input_stream: AsyncIterator[QueuedItem],
        resume_session_id: str | None,
    ) -> AsyncIterator[AgentEvent]:
        self._project = project
        self._conversation = conversation
        # The burst's tool list comes from its profile, as under the SDK backend (C5).
        self._burst_specs = profile_specs(conversation.type, self._specs)
        names = [s.name for s in self._burst_specs]
        record = FakeBurst(conversation.id, conversation.type, names)
        self.bursts.append(record)
        self.conversations.append(conversation.id)
        self.resume_ids.append(resume_session_id)
        self._burst += 1
        yield SessionId(f"fake-session-{self._burst}")
        async for item in input_stream:
            self.prompts.append(item)
            record.prompts.append(item.text)
            self._interrupted.clear()
            async for ev in self._run(item):
                yield ev

    def _kicadless_ctx_factory(self) -> Callable[[], ToolContext] | None:
        project, settings, conversation = self._project, self._settings, self._conversation
        turn_context = self._turn_context
        if turn_context is not None:
            # The engine's KiCad-less context: run id, docs, doc notices (S-C3, S-C5).
            return lambda: bind_conversation(turn_context(), conversation)
        if project is None or settings is None:
            return None

        def make() -> ToolContext:
            ctx = ToolContext(
                project=project, session=cast("KicadSession", None), settings=settings
            )
            return bind_conversation(ctx, conversation)

        return make

    def _bound(self, factory: Callable[[], ToolContext]) -> Callable[[], ToolContext]:
        conversation = self._conversation
        return lambda: bind_conversation(factory(), conversation)

    def _expand(self, steps: list[Step]) -> list[Step]:
        """Flatten Delegate steps into Emit/Wait steps (interrupt checks stay per step)."""
        out: list[Step] = []
        for step in steps:
            if not isinstance(step, Delegate):
                out.append(step)
                continue
            parent = f"fake-tool-{next(self._tool_ids)}"
            agent_input = {
                "subagent_type": step.subagent_type,
                "description": step.description,
                "prompt": step.description,
            }
            out.append(Emit(ToolUse(id=parent, name="Agent", input=agent_input)))
            for sub in step.steps:
                if isinstance(sub, Text):
                    out.append(Emit(SubagentText(parent_tool_use_id=parent, text=sub.text)))
                elif isinstance(sub, Tool):
                    tid = f"fake-tool-{next(self._tool_ids)}"
                    out.append(
                        Emit(
                            ToolUse(
                                id=tid,
                                name=sub.name,
                                input=dict(sub.args),
                                parent_tool_use_id=parent,
                            )
                        )
                    )
                    out.append(
                        Emit(
                            ToolResultEvt(
                                id=tid, content=f"{sub.name} ok", parent_tool_use_id=parent
                            )
                        )
                    )
                else:
                    out.append(sub)
            out.append(Emit(ToolResultEvt(id=parent, content=step.summary)))
        return out

    async def _run(self, item: QueuedItem) -> AsyncIterator[AgentEvent]:
        for step in self._expand(self._script_for(item)):
            if self._interrupted.is_set():
                yield TurnFinished(reason="interrupted")
                return
            if isinstance(step, Text):
                yield TextDelta(step.text)
            elif isinstance(step, Think):
                yield ThinkingDelta(step.text)
            elif isinstance(step, Emit):
                yield step.event
            elif isinstance(step, Finish):
                yield Usage(
                    input_tokens=1, output_tokens=1, cache_read_tokens=0, cache_creation_tokens=0
                )
                yield TurnFinished(reason=step.reason, error=step.error)
                return
            elif isinstance(step, Wait):
                waiters = {
                    asyncio.ensure_future(step.event.wait()),
                    asyncio.ensure_future(self._interrupted.wait()),
                }
                _, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                for p in pending:
                    p.cancel()
            elif isinstance(step, Tool):
                tool_id = f"fake-tool-{next(self._tool_ids)}"
                yield ToolUse(id=tool_id, name=sdk_tool_name(step.name), input=dict(step.args))
                # The main-agent guard of the burst's profile, as under the SDK backend: a
                # refused call is an ordinary error result (C5).
                allowed = frozenset(sdk_tool_name(s.name) for s in self._burst_specs)
                verdict = await make_main_guard(allowed)(
                    sdk_tool_name(step.name), dict(step.args), ToolPermissionContext()
                )
                if isinstance(verdict, PermissionResultDeny):
                    yield ToolResultEvt(id=tool_id, content=verdict.message, is_error=True)
                    continue
                ctx_factory = self._ctx_factory
                if ctx_factory is None:
                    raise RuntimeError("FakeAgent Tool step needs ctx_factory")
                bound = self._bound(ctx_factory)

                tools = {
                    t.name: t
                    for t in build_sdk_tool_list(
                        self._burst_specs, bound, self._kicadless_ctx_factory()
                    )
                }
                raw = await tools[step.name].handler(dict(step.args))
                yield mcp_result_to_event(tool_id, raw)
        if self._interrupted.is_set():
            yield TurnFinished(reason="interrupted")
            return
        yield Usage(input_tokens=1, output_tokens=1, cache_read_tokens=0, cache_creation_tokens=0)
        yield TurnFinished(reason="completed")
