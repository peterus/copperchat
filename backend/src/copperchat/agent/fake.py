# backend/src/copperchat/agent/fake.py
"""Scriptable in-process AgentBackend for tests and dev_fake_agent=True (no token needed)."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from copperchat.agent.events import (
    AgentEvent,
    QueuedItem,
    SessionId,
    TextDelta,
    ThinkingDelta,
    ToolUse,
    TurnFinished,
    Usage,
)
from copperchat.agent.sdk_tools import build_sdk_tool_list, mcp_result_to_event, sdk_tool_name
from copperchat.projects import Project
from copperchat.tools import ToolContext, ToolSpec


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


Step = Text | Think | Tool | Wait | Emit | Finish
ScriptFor = Callable[[QueuedItem], list[Step]]


def echo_script(item: QueuedItem) -> list[Step]:
    return [Text("You said: "), Text(item.text)]


class FakeAgent:
    def __init__(
        self,
        script_for: ScriptFor | None = None,
        *,
        specs: Sequence[ToolSpec] = (),
        ctx_factory: Callable[[], ToolContext] | None = None,
        ignore_interrupt: bool = False,
    ) -> None:
        self._script_for = script_for or echo_script
        self._specs = list(specs)
        self._ctx_factory = ctx_factory
        self._ignore_interrupt = ignore_interrupt
        self._interrupted = asyncio.Event()
        self._tool_ids = itertools.count(1)
        self._burst = 0
        self.prompts: list[QueuedItem] = []
        self.resume_ids: list[str | None] = []
        self.interrupts = 0

    async def interrupt(self) -> None:
        self.interrupts += 1
        if not self._ignore_interrupt:
            self._interrupted.set()

    async def start(
        self,
        project: Project,
        input_stream: AsyncIterator[QueuedItem],
        resume_session_id: str | None,
    ) -> AsyncIterator[AgentEvent]:
        self.resume_ids.append(resume_session_id)
        self._burst += 1
        yield SessionId(f"fake-session-{self._burst}")
        async for item in input_stream:
            self.prompts.append(item)
            self._interrupted.clear()
            async for ev in self._run(item):
                yield ev

    async def _run(self, item: QueuedItem) -> AsyncIterator[AgentEvent]:
        for step in self._script_for(item):
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
                if self._ctx_factory is None:
                    raise RuntimeError("FakeAgent Tool step needs ctx_factory")
                tools = {t.name: t for t in build_sdk_tool_list(self._specs, self._ctx_factory)}
                raw = await tools[step.name].handler(dict(step.args))
                yield mcp_result_to_event(tool_id, raw)
        if self._interrupted.is_set():
            yield TurnFinished(reason="interrupted")
            return
        yield Usage(input_tokens=1, output_tokens=1, cache_read_tokens=0, cache_creation_tokens=0)
        yield TurnFinished(reason="completed")
