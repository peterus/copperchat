"""Engine bursts bound to conversations (spec §5.2-§5.3, plan S-C3, Review Focus 1)."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from copperchat.agent.engine import ChainedHooks, RunHooks, TurnInfo, TurnOutcome
from copperchat.agent.events import AgentEvent, QueuedItem, RateLimited
from copperchat.agent.fake import Emit, FakeAgent, Finish, Text, Tool, Wait
from copperchat.db.repos.agent_state import RateLimitRepo
from copperchat.db.repos.conversations import ConversationRepo, ConversationRow
from copperchat.db.repos.messages import MessageRepo
from copperchat.errors import ConversationClosed, ConversationNotFound
from copperchat.projects import Project
from copperchat.tools import ToolContext, ToolResult, ToolSpec

PID = "a1b2c3d4e5f6"
NOTICE = "[CopperChat notice]"


async def _conv(db: Any, title: str | None = None, pid: str = PID) -> str:
    return (await ConversationRepo(db).create(pid, "free", title)).id


async def _status(db: Any, mid: int) -> str:
    row = await MessageRepo(db).get(mid)
    assert row is not None
    return row.status


def _by_text(agent: Any) -> dict[str, QueuedItem]:
    return {p.text.split("\n\n")[-1]: p for p in agent.prompts}


class _SlowEnd(FakeAgent):
    """Winds down slowly: after its input ended, the backend returns only once
    ``end_gate`` is set (like a CLI that takes a while to exit)."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.end_gate = asyncio.Event()
        self.winding_down = 0

    async def start(  # type: ignore[override]
        self,
        project: Project,
        conversation: ConversationRow,
        input_stream: AsyncIterator[QueuedItem],
        resume_session_id: str | None,
    ) -> AsyncIterator[AgentEvent]:
        async for ev in super().start(project, conversation, input_stream, resume_session_id):
            yield ev
        self.winding_down += 1
        await self.end_gate.wait()
        self.winding_down -= 1


class _GatedStart(FakeAgent):
    """Connects slowly: the burst exists but pulls no input until ``start_gate`` is set
    (a burst between turns: no item in flight)."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.start_gate = asyncio.Event()
        self.connecting = 0

    async def start(  # type: ignore[override]
        self,
        project: Project,
        conversation: ConversationRow,
        input_stream: AsyncIterator[QueuedItem],
        resume_session_id: str | None,
    ) -> AsyncIterator[AgentEvent]:
        self.connecting += 1
        await self.start_gate.wait()
        self.connecting -= 1
        async for ev in super().start(project, conversation, input_stream, resume_session_id):
            yield ev


class _Recorder(RunHooks):
    def __init__(self, extra: list[str] | None = None, fail_input: bool = False) -> None:
        self.turns: list[TurnInfo] = []
        self.outcomes: list[TurnOutcome] = []
        self.failed: list[tuple[str, list[int], str]] = []
        self.extra = extra or []
        self.fail_input = fail_input

    async def on_turn_start(self, turn: TurnInfo) -> None:
        self.turns.append(turn)

    async def before_model_input(self, turn: TurnInfo) -> list[str]:
        if self.fail_input:
            raise RuntimeError("injection failed")
        return list(self.extra)

    async def on_turn_end(self, turn: TurnInfo, outcome: TurnOutcome) -> None:
        self.outcomes.append(outcome)

    async def on_items_failed(self, conversation_id: str, message_ids: list[int], status: str) -> None:
        self.failed.append((conversation_id, list(message_ids), status))


def _gated(gates: dict[str, asyncio.Event]) -> Any:
    def script(item: QueuedItem) -> list[Any]:
        text = item.text.split("\n\n")[-1]
        gate = gates.get(text)
        return [Wait(gate), Text(f"re {text}")] if gate is not None else [Text(f"re {text}")]

    return script


# ---- ordering (Review Focus 1)
async def test_a_b_a_three_bursts_in_order(make_engine, db, until):
    a, b = await _conv(db), await _conv(db)
    gate = asyncio.Event()
    engine, agent = await make_engine(_gated({"A1": gate}))
    await engine.submit_ex(a, "a1", "A1", [])
    await until(lambda: len(agent.prompts) == 1)
    mb, _ = await engine.submit_ex(b, "b", "B", [])
    ma3, _ = await engine.submit_ex(a, "a3", "A3", [])
    await asyncio.sleep(0.05)
    assert len(agent.prompts) == 1, "neither B nor A3 joins A1's burst"
    assert await _status(db, mb.id) == "queued" and await _status(db, ma3.id) == "queued"
    gate.set()
    await until(lambda: len(agent.prompts) == 3 and not engine.is_busy)
    assert [p.text for p in agent.prompts] == ["A1", "B", "A3"]
    assert agent.conversations == [a, b, a], "three bursts, one conversation each"
    # Each burst resumes its own conversation's SDK session.
    assert agent.resume_ids == [None, None, "fake-session-1"]
    assert (await ConversationRepo(db).get(a)).sdk_session_id == "fake-session-3"  # type: ignore[union-attr]
    assert (await ConversationRepo(db).get(b)).sdk_session_id == "fake-session-2"  # type: ignore[union-attr]


async def test_a3_after_drain_before_finally(make_engine, db, until):
    a, b = await _conv(db), await _conv(db)
    gate = asyncio.Event()
    engine, agent = await make_engine(
        _gated({"A1": gate}), backend_cls=_SlowEnd, interrupt_grace_s=5.0
    )
    await engine.submit_ex(a, "a1", "A1", [])
    await until(lambda: len(agent.prompts) == 1)
    await engine.submit_ex(b, "b", "B", [])  # seals A's stream
    gate.set()
    await until(lambda: agent.winding_down == 1)  # A's input drained, burst not ended
    ma3, _ = await engine.submit_ex(a, "a3", "A3", [])
    await asyncio.sleep(0.05)
    assert await _status(db, ma3.id) == "queued"
    assert len(agent.prompts) == 1
    agent.end_gate.set()
    await until(lambda: len(agent.prompts) == 3 and not engine.is_busy)
    assert [p.text for p in agent.prompts] == ["A1", "B", "A3"], "B still goes first"
    assert agent.conversations == [a, b, a]


async def test_seal_during_wind_down(make_engine, db, until):
    a, b = await _conv(db), await _conv(db)
    g3 = asyncio.Event()
    engine, agent = await make_engine(
        _gated({"A3": g3}), backend_cls=_SlowEnd, interrupt_grace_s=5.0
    )
    await engine.submit_ex(a, "a1", "A1", [])
    await until(lambda: agent.winding_down == 1)  # A1 done, its burst winds down
    old_burst = engine._burst
    await engine.submit_ex(a, "a3", "A3", [])  # new stream S2, waits in _await_wind_down
    s2 = engine._stream
    assert s2 is not None and engine._burst is not old_burst
    mb, _ = await engine.submit_ex(b, "b", "B", [])  # seals S2
    assert engine._sealed_stream is s2
    agent.end_gate.set()
    await until(lambda: old_burst is not None and old_burst.done())
    await until(lambda: len(agent.prompts) == 2)  # A3 runs in S2's burst
    assert engine._sealed_stream is s2, "the old burst's finally must not unseal S2"
    ma4, _ = await engine.submit_ex(a, "a4", "A4", [])
    await asyncio.sleep(0.05)
    assert await _status(db, mb.id) == "queued" and await _status(db, ma4.id) == "queued"
    g3.set()
    await until(lambda: len(agent.prompts) == 4 and not engine.is_busy)
    assert [p.text for p in agent.prompts] == ["A1", "A3", "B", "A4"]
    assert agent.conversations == [a, a, b, a]
    assert engine._sealed_stream is None


# ---- session ids and cancellation
async def test_session_id_stored_on_burst_conversation(make_engine, db, until):
    a, b = await _conv(db), await _conv(db)
    engine, agent = await make_engine()
    await engine.submit_ex(a, "a1", "A1", [])
    await until(lambda: len(agent.prompts) == 1 and not engine.is_busy)
    await engine.submit_ex(b, "b1", "B1", [])
    await until(lambda: len(agent.prompts) == 2 and not engine.is_busy)
    repo = ConversationRepo(db)
    assert (await repo.get(a)).sdk_session_id == "fake-session-1"  # type: ignore[union-attr]
    assert (await repo.get(b)).sdk_session_id == "fake-session-2"  # type: ignore[union-attr]


async def test_stop_during_sealed_stream(make_engine, db, until):
    a, b = await _conv(db), await _conv(db)
    never = asyncio.Event()
    hooks = _Recorder()
    engine, agent = await make_engine(_gated({"A1": never}), hooks=hooks)
    await engine.submit_ex(a, "a1", "A1", [])
    await until(lambda: engine.current_run_id is not None)
    mb, _ = await engine.submit_ex(b, "b", "B", [])
    ma3, _ = await engine.submit_ex(a, "a3", "A3", [])
    await engine.interrupt(engine.current_run_id, b)  # a running turn wins over the frame
    await until(lambda: len(agent.prompts) == 2 and not engine.is_busy)
    assert [p.text for p in agent.prompts] == ["A1", "B"]
    assert await _status(db, ma3.id) == "cancelled"
    assert await _status(db, mb.id) == "done"
    assert (a, [ma3.id], "cancelled") in hooks.failed


async def test_stop_from_b_view_on_a_between_turns(make_engine, db, until):
    a, b = await _conv(db), await _conv(db)
    engine, agent = await make_engine(backend_cls=_GatedStart)
    ma1, _ = await engine.submit_ex(a, "a1", "A1", [])
    await until(lambda: agent.connecting == 1)  # burst for A, nothing pulled yet
    mb, _ = await engine.submit_ex(b, "b", "B", [])
    assert engine.current_run_id is None
    # The running chip in B's view sends A's conversationId.
    await engine.interrupt(None, a)
    assert await _status(db, ma1.id) == "cancelled"
    assert await _status(db, mb.id) == "queued"
    agent.start_gate.set()
    await until(lambda: len(agent.prompts) == 1 and not engine.is_busy)
    assert [p.text for p in agent.prompts] == ["B"]
    assert await _status(db, mb.id) == "done"


async def test_stop_for_other_conversation_between_turns_leaves_stream(make_engine, db, until):
    a, b = await _conv(db), await _conv(db)
    engine, agent = await make_engine(backend_cls=_GatedStart)
    ma1, _ = await engine.submit_ex(a, "a1", "A1", [])
    await until(lambda: agent.connecting == 1)
    mb, _ = await engine.submit_ex(b, "b", "B", [])
    await engine.interrupt(None, b)
    assert await _status(db, mb.id) == "cancelled"
    assert await _status(db, ma1.id) == "queued"
    agent.start_gate.set()
    await until(lambda: len(agent.prompts) == 1 and not engine.is_busy)
    assert [p.text for p in agent.prompts] == ["A1"]


async def test_turn_context_in_kicadless_ctx(make_engine, db, until):
    a = await _conv(db)
    seen: list[str | None] = []

    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        seen.append(ctx.run_id)
        return ToolResult(text="ok")

    spec = ToolSpec(
        name="kless",
        description="d",
        input_schema={"type": "object"},
        handler=handler,
        requires_kicad=False,
    )
    engine, agent = await make_engine(lambda item: [Tool("kless")], specs=[spec])
    m, _ = await engine.submit_ex(a, "a1", "A1", [])  # with_session=False: KiCad stopped
    await until(lambda: len(agent.prompts) == 1 and not engine.is_busy)
    row = await MessageRepo(db).get(m.id)
    assert row is not None and row.run_id is not None
    assert seen == [row.run_id]


async def test_stop_scoped(make_engine, db, until):
    a, b = await _conv(db), await _conv(db)
    never = asyncio.Event()
    hooks = _Recorder()
    engine, agent = await make_engine(_gated({"A1": never}), hooks=hooks)
    await engine.submit_ex(a, "a1", "A1", [])
    await until(lambda: engine.current_run_id is not None)
    ma2, _ = await engine.submit_ex(a, "a2", "A2", [])  # in A's stream
    mb, _ = await engine.submit_ex(b, "b", "B", [])  # seals; DB-queued
    ma3, _ = await engine.submit_ex(a, "a3", "A3", [])  # DB-queued (sealed)
    run_id = engine.current_run_id
    await engine.interrupt(run_id, a)
    assert await _status(db, ma2.id) == "cancelled"
    assert await _status(db, ma3.id) == "cancelled"
    assert await _status(db, mb.id) == "queued", "B's message is not A's to cancel"
    await until(lambda: len(agent.prompts) == 2 and not engine.is_busy)
    assert [p.text for p in agent.prompts] == ["A1", "B"]
    assert await _status(db, mb.id) == "done"
    failed = [f for f in hooks.failed if f[2] == "cancelled"]
    assert failed == [(a, sorted([ma2.id, ma3.id]), "cancelled")]
    assert hooks.outcomes[0].reason == "interrupted"
    assert hooks.outcomes[0].conversation_id == a


async def test_stop_between_turns_uses_frame_conversation(make_engine, db, until):
    a, b = await _conv(db), await _conv(db)
    await RateLimitRepo(db).set(PID, datetime.now(UTC) + timedelta(seconds=0.3))
    engine, agent = await make_engine()
    assert engine.rate_limited_until is not None
    ma, _ = await engine.submit_ex(a, "a1", "A1", [])
    mb, _ = await engine.submit_ex(b, "b1", "B1", [])
    await engine.interrupt(None, b)
    assert await _status(db, mb.id) == "cancelled"
    assert await _status(db, ma.id) == "queued"
    await until(lambda: len(agent.prompts) == 1 and not engine.is_busy, timeout=3.0)
    assert [p.text for p in agent.prompts] == ["A1"]


async def test_cancel_db_queued_foreign_row(make_engine, db, until):
    a, b = await _conv(db), await _conv(db)
    gate = asyncio.Event()
    hooks = _Recorder()
    engine, agent = await make_engine(_gated({"A1": gate}), hooks=hooks)
    await engine.submit_ex(a, "a1", "A1", [])
    await until(lambda: engine.current_run_id is not None)
    mb, _ = await engine.submit_ex(b, "b", "B", [])
    assert await engine.cancel_queued(mb.id) is True
    assert await _status(db, mb.id) == "cancelled"
    assert hooks.failed == [(b, [mb.id], "cancelled")]
    gate.set()
    await until(lambda: not engine.is_busy)
    await asyncio.sleep(0.05)
    assert [p.text for p in agent.prompts] == ["A1"]


# ---- restart, rate limits and notes
async def test_restart_two_conversations(make_engine, db, until):
    a, b = await _conv(db), await _conv(db)
    repo = MessageRepo(db)
    ids = [
        (await repo.insert_user(PID, a, "a1", "A1"))[0].id,
        (await repo.insert_user(PID, b, "b1", "B1"))[0].id,
        (await repo.insert_user(PID, a, "a2", "A2"))[0].id,
    ]
    engine, agent = await make_engine()  # start() re-drives the queued rows
    await until(lambda: len(agent.prompts) == 3 and not engine.is_busy)
    assert [p.text for p in agent.prompts] == ["A1", "B1", "A2"]
    assert [p.message_id for p in agent.prompts] == ids
    assert agent.conversations == [a, b, a]


async def test_rate_limit_parks_all(make_engine, db, until):
    a, b = await _conv(db), await _conv(db)
    gate = asyncio.Event()
    seen = {"A1": 0}

    def script(item: QueuedItem) -> list[Any]:
        if item.text == "A1":
            seen["A1"] += 1
            if seen["A1"] == 1:
                resets = datetime.now(UTC) + timedelta(seconds=0.4)
                return [Wait(gate), Emit(RateLimited(resets_at=resets)), Finish("error", "rl")]
        return [Text(f"re {item.text}")]

    engine, agent = await make_engine(script)
    m1, _ = await engine.submit_ex(a, "a1", "A1", [])
    await until(lambda: len(agent.prompts) == 1)
    m2, _ = await engine.submit_ex(a, "a2", "A2", [])
    m3, _ = await engine.submit_ex(b, "b1", "B1", [])
    gate.set()
    await until(lambda: engine.rate_limited_until is not None and not engine.is_busy)
    for m in (m1, m2, m3):
        assert await _status(db, m.id) == "queued"
    await until(lambda: len(agent.prompts) == 4 and not engine.is_busy, timeout=3.0)
    assert [p.text for p in agent.prompts] == ["A1", "A1", "A2", "B1"]
    assert agent.conversations == [a, a, b]


async def test_broadcast_note_reaches_each_open_conversation_once(make_engine, db, until):
    a, b = await _conv(db), await _conv(db)
    engine, agent = await make_engine()
    engine.add_agent_note("the design changed")
    await asyncio.sleep(0.01)  # millisecond timestamps: c is created after the note
    c = await _conv(db)
    for cid, text in ((a, "A1"), (a, "A2"), (b, "B1"), (b, "B2"), (c, "C1")):
        await engine.submit_ex(cid, text.lower(), text, [])
        await until(lambda t=text: any(p.text.endswith(t) for p in agent.prompts))
        await until(lambda: not engine.is_busy)
    texts = _by_text(agent)
    assert texts["A1"].text == f"{NOTICE} the design changed\n\nA1"
    assert texts["A2"].text == "A2"
    assert texts["B1"].text == f"{NOTICE} the design changed\n\nB1"
    assert texts["B2"].text == "B2"
    assert texts["C1"].text == "C1", "a session opened after the note never saw the old state"


async def test_conversation_note_only_there(make_engine, db, until):
    a, b = await _conv(db), await _conv(db)
    engine, agent = await make_engine()
    engine.add_agent_note("ERC found 2 errors", conversation_id=a)
    await engine.submit_ex(b, "b1", "B1", [])
    await until(lambda: len(agent.prompts) == 1 and not engine.is_busy)
    await engine.submit_ex(a, "a1", "A1", [])
    await until(lambda: len(agent.prompts) == 2 and not engine.is_busy)
    assert [p.text for p in agent.prompts] == ["B1", f"{NOTICE} ERC found 2 errors\n\nA1"]


# ---- notices, closed sessions and hooks
async def test_post_notice_into_given_conversation(make_engine, db, drain):
    a, b = await _conv(db), await _conv(db)
    engine, _ = await make_engine()
    sub = engine.broadcaster.subscribe(PID)
    dto = await engine.post_notice(b, "hello", {"action": "x"})
    err = await engine.post_error(a, "bad", {"code": "y"})
    assert dto.conversation_id == b and err.conversation_id == a
    row = await MessageRepo(db).get(dto.id)
    assert row is not None and row.conversation_id == b and row.kind == "notice"
    frames = [f for f in await drain(sub) if f.type == "message"]
    assert [(f.message.conversation_id, f.message.kind) for f in frames] == [
        (b, "notice"),
        (a, "error"),
    ]
    with pytest.raises(ConversationNotFound):
        await engine.post_notice(await _conv(db, pid="ffffffffffff"), "x")


async def test_send_to_closed_conversation_refused(make_engine, db, until):
    a = await _conv(db)
    engine, agent = await make_engine()
    repo = ConversationRepo(db)
    await repo.set_status(a, "closing", expect="open")
    with pytest.raises(ConversationClosed) as err:
        await engine.submit_ex(a, "c1", "hi", [])
    assert err.value.code == "conversation_closed"
    # The handover turn itself may be submitted while the session is closing (S-C3).
    dto, created = await engine.submit_ex(a, "c2", "handover", [], {"origin": "close-session"})
    assert created and dto.conversation_id == a
    await until(lambda: len(agent.prompts) == 1 and not engine.is_busy)
    await repo.set_status(a, "closed", expect="closing")
    with pytest.raises(ConversationClosed):
        await engine.submit_ex(a, "c3", "again", [], {"origin": "close-session"})
    foreign = await _conv(db, pid="ffffffffffff")
    with pytest.raises(ConversationNotFound):
        await engine.submit_ex(foreign, "c4", "hi", [])
    rows = await MessageRepo(db).queued_user_rows(PID)
    assert rows == []


async def test_before_model_input_prefixes_each_entry_once(make_engine, db, until):
    a = await _conv(db)
    hooks = _Recorder(extra=["index:\n- requirements.md (draft)", "second"])
    engine, agent = await make_engine(hooks=hooks)
    engine.add_agent_note("a note")
    await engine.submit_ex(a, "a1", "the user text", [])
    await until(lambda: len(agent.prompts) == 1 and not engine.is_busy)
    text = agent.prompts[0].text
    assert text == (
        f"{NOTICE} a note\n"
        f"{NOTICE} index:\n- requirements.md (draft)\n"
        f"{NOTICE} second\n\n"
        "the user text"
    )
    assert text.count(NOTICE) == 3
    assert hooks.turns[0].text == "the user text"
    assert hooks.turns[0].conversation_id == a


async def test_failing_before_model_input_does_not_fail_the_turn(make_engine, db, until):
    a = await _conv(db)
    hooks = _Recorder(extra=["never"], fail_input=True)
    engine, agent = await make_engine(hooks=hooks)
    await engine.submit_ex(a, "a1", "hi", [])
    await until(lambda: len(agent.prompts) == 1 and not engine.is_busy)
    assert agent.prompts[0].text == "hi"
    assert hooks.outcomes[0].reason == "completed"


class _BrokenBackend:
    def __init__(self) -> None:
        self.prompts: list[QueuedItem] = []

    async def interrupt(self) -> None:
        return None

    async def start(
        self,
        project: Project,
        conversation: ConversationRow,
        input_stream: AsyncIterator[QueuedItem],
        resume: str | None,
    ) -> AsyncIterator[AgentEvent]:
        raise RuntimeError("bad credentials")
        yield  # pragma: no cover


async def test_on_items_failed_from_fail_burst(make_engine, db, until):
    a = await _conv(db)
    hooks = _Recorder()
    engine, _ = await make_engine(backend=_BrokenBackend(), hooks=hooks)
    m1, _ = await engine.submit_ex(a, "a1", "A1", [])
    await until(lambda: not engine.is_busy)
    assert await _status(db, m1.id) == "error"
    assert hooks.failed == [(a, [m1.id], "error")]
    rows, _ = await MessageRepo(db).page(PID, a, limit=10)
    assert rows[-1].kind == "error" and rows[-1].conversation_id == a


async def test_turn_outcome_and_frames_carry_the_conversation(make_engine, db, until, drain):
    a = await _conv(db)
    hooks = _Recorder()
    engine, agent = await make_engine(hooks=hooks)
    sub = engine.broadcaster.subscribe(PID)
    await engine.submit_ex(a, "a1", "A1", [])
    await until(lambda: len(agent.prompts) == 1 and not engine.is_busy)
    frames = await drain(sub)
    for f in frames:
        if f.type in ("message-delta", "run-started", "run-finished", "usage"):
            assert f.conversation_id == a, f.type
        if f.type == "message":
            assert f.message.conversation_id == a
    updates = [f for f in frames if f.type == "conversation-updated"]
    assert len(updates) >= 2, "first message and the turn's end"
    assert updates[0].conversation.id == a
    assert updates[-1].conversation.usage.input_tokens == 1
    assert hooks.outcomes[0].conversation_id == a
    assert hooks.outcomes[0].message_status == "done"
    assert engine.current_conversation_id is None


async def test_current_conversation_id_during_turn(make_engine, db, until):
    a = await _conv(db)
    gate = asyncio.Event()
    engine, agent = await make_engine(_gated({"A1": gate}))
    await engine.submit_ex(a, "a1", "A1", [])
    await until(lambda: engine.current_run_id is not None)
    assert engine.current_conversation_id == a
    _, _, run_id, cid = await engine.replay_and_subscribe(a, 0)
    assert (run_id, cid) == (engine.current_run_id, a)
    gate.set()
    await until(lambda: not engine.is_busy)


async def test_replay_is_per_conversation(make_engine, db):
    a, b = await _conv(db), await _conv(db)
    engine, _ = await make_engine()
    na = await engine.post_notice(a, "for a")
    await engine.post_notice(b, "for b")
    dtos, sub, _, _ = await engine.replay_and_subscribe(a, 0)
    sub.close()
    assert [d.id for d in dtos] == [na.id]


# ---- ChainedHooks (S-C3)
class _H(RunHooks):
    def __init__(self, name: str, log: list[str], *, fail: bool = False) -> None:
        self.name, self.log, self.fail = name, log, fail

    async def before_model_input(self, turn: TurnInfo) -> list[str]:
        self.log.append(f"input:{self.name}")
        if self.fail:
            raise RuntimeError(self.name)
        return [f"from {self.name}"]

    async def on_items_failed(self, conversation_id: str, message_ids: list[int], status: str) -> None:
        self.log.append(f"failed:{self.name}")
        if self.fail:
            raise RuntimeError(self.name)


_TURN = TurnInfo(run_id="r", message_id=1, text="t", metadata={}, conversation_id="c" * 32)


async def test_chained_hooks_concatenate_before_model_input() -> None:
    log: list[str] = []
    chain = ChainedHooks([_H("a", log), _H("b", log)])
    assert await chain.before_model_input(_TURN) == ["from a", "from b"]
    assert await RunHooks().before_model_input(_TURN) == []


async def test_chained_before_model_input_stops_at_first_error() -> None:
    log: list[str] = []
    chain = ChainedHooks([_H("a", log, fail=True), _H("b", log)])
    with pytest.raises(RuntimeError, match="a"):
        await chain.before_model_input(_TURN)
    assert log == ["input:a"]


async def test_chained_on_items_failed_runs_all_then_raises_first() -> None:
    log: list[str] = []
    chain = ChainedHooks([_H("a", log, fail=True), _H("b", log, fail=True), _H("c", log)])
    with pytest.raises(RuntimeError, match="a"):
        await chain.on_items_failed("c" * 32, [1], "cancelled")
    assert log == ["failed:a", "failed:b", "failed:c"]
