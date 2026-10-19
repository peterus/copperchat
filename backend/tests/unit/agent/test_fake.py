# backend/tests/unit/agent/test_fake.py
import asyncio
from typing import Any, cast

from copperchat.agent.events import (
    QueuedItem,
    SessionId,
    TextDelta,
    ToolResultEvt,
    ToolUse,
    TurnFinished,
    Usage,
)
from copperchat.agent.fake import FakeAgent, Finish, Text, Tool, Wait
from copperchat.tools import ToolContext, ToolResult, ToolSpec
from tests.support.conversations import conversation_row

PROJECT = cast(Any, None)


async def _items(*texts: str):  # type: ignore[no-untyped-def]
    for i, t in enumerate(texts, 1):
        yield QueuedItem(message_id=i, client_message_id=f"c{i}", conversation_id="c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0", text=t)


async def test_default_script_echoes_and_reports_usage():
    agent = FakeAgent()
    events = [e async for e in agent.start(PROJECT, conversation_row(), _items("hi"), "s-0")]
    assert events[0] == SessionId("fake-session-1")
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "You said: hi"
    assert isinstance(events[-2], Usage) and events[-1] == TurnFinished(reason="completed")
    assert agent.resume_ids == ["s-0"] and [p.text for p in agent.prompts] == ["hi"]


async def test_tool_step_calls_registered_handler():
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        return ToolResult(data={"n": args["n"]})

    spec = ToolSpec(name="probe", description="d", input_schema={"type": "object"}, handler=handler)
    agent = FakeAgent(
        lambda item: [Tool("probe", {"n": 2})],
        specs=[spec],
        ctx_factory=lambda: cast(ToolContext, None),
    )
    events = [e async for e in agent.start(PROJECT, conversation_row(), _items("x"), None)]
    use = next(e for e in events if isinstance(e, ToolUse))
    res = next(e for e in events if isinstance(e, ToolResultEvt))
    assert use.name == "mcp__copperchat__probe" and res.id == use.id
    assert res.content == '{"n": 2}' and res.is_error is False


async def test_interrupt_ends_waiting_turn():
    gate = asyncio.Event()
    agent = FakeAgent(lambda item: [Text("a"), Wait(gate), Text("never")])
    gen = agent.start(PROJECT, conversation_row(), _items("x"), None)
    assert await gen.__anext__() == SessionId("fake-session-1")
    assert await gen.__anext__() == TextDelta("a")
    nxt = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.01)
    await agent.interrupt()
    assert await asyncio.wait_for(nxt, 0.5) == TurnFinished(reason="interrupted")
    assert agent.interrupts == 1


async def test_ignore_interrupt_keeps_waiting():
    gate = asyncio.Event()
    agent = FakeAgent(lambda item: [Wait(gate), Finish()], ignore_interrupt=True)
    gen = agent.start(PROJECT, conversation_row(), _items("x"), None)
    await gen.__anext__()
    nxt = asyncio.ensure_future(gen.__anext__())
    await agent.interrupt()
    await asyncio.sleep(0.05)
    assert not nxt.done()
    nxt.cancel()
