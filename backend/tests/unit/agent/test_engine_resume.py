import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from copperchat.agent.events import AgentEvent, QueuedItem, RateLimited, SessionId, TextDelta
from copperchat.agent.fake import Emit, Finish, Text, Tool, Wait
from copperchat.db.repos.agent_state import AgentSessionRepo, RateLimitRepo, RunRepo
from copperchat.db.repos.messages import MessageRepo
from copperchat.kicad_session import KicadUnavailable
from copperchat.projects import Project
from copperchat.tools import ToolContext, ToolResult, ToolSpec

PID = "a1b2c3d4e5f6"


async def test_start_finalizes_runs_left_open_by_a_crash(make_engine, db):
    await RunRepo(db).start("crashed", PID)
    await make_engine(lambda item: [Text("x")])
    run = await RunRepo(db).get("crashed")
    assert run is not None and run["finished_at"] is not None and run["reason"] == "interrupted"


async def test_session_id_stored_and_resume_only_at_burst_start(make_engine, db, until):
    gate = asyncio.Event()
    engine, agent = await make_engine(
        lambda item: [Wait(gate), Text("1")] if item.text == "m1" else [Text("n")]
    )
    await engine.submit_ex("a", "m1", [])
    await until(lambda: len(agent.prompts) == 1)
    await engine.submit_ex("b", "m2", [])  # joins the running burst
    gate.set()
    await until(lambda: len(agent.prompts) == 2 and not engine.is_busy)
    assert agent.resume_ids == [None], "no resume mid-burst"
    assert await AgentSessionRepo(db).get(PID) == "fake-session-1"
    await engine.submit_ex("c", "m3", [])  # new burst
    await until(lambda: len(agent.prompts) == 3 and not engine.is_busy)
    assert agent.resume_ids == [None, "fake-session-1"]
    assert await AgentSessionRepo(db).get(PID) == "fake-session-2"


async def test_rate_limit_parks_publishes_and_auto_resumes(make_engine, db, until, drain):
    gate = asyncio.Event()
    seen = {"m1": 0}

    def script(item: Any) -> list[Any]:
        if item.text == "m1":
            seen["m1"] += 1
            if seen["m1"] == 1:
                resets = datetime.now(UTC) + timedelta(seconds=0.4)
                return [
                    Text("partial"),
                    Wait(gate),
                    Emit(RateLimited(resets_at=resets)),
                    Finish("error", "rate limited"),
                ]
        return [Text(f"done {item.text}")]

    engine, agent = await make_engine(script)
    sub = engine.broadcaster.subscribe(PID)
    m1, _ = await engine.submit_ex("a", "m1", [])
    await until(lambda: len(agent.prompts) == 1)
    m2, _ = await engine.submit_ex("b", "m2", [])
    gate.set()
    # Wait for the rate-limited frame, the last step of parking: rate_limited_until is
    # set *first* (so nothing new is enqueued), before the message metadata, the
    # status and the persisted limit are written - polling it raced those writes.
    frames: list[Any] = []
    async with asyncio.timeout(2.0):
        while not any(f.type == "rate-limited" for f in frames):
            frames.append(await sub.__anext__())
    assert engine.rate_limited_until is not None
    repo = MessageRepo(db)
    r1, r2 = await repo.get(m1.id), await repo.get(m2.id)
    assert r1 is not None and r1.status == "queued" and r1.metadata["rateLimited"] is True
    assert r2 is not None and r2.status == "queued"
    assert await RateLimitRepo(db).get(PID) == engine.rate_limited_until
    await engine.submit_ex("c", "m3", [])  # stays queued while parked
    await asyncio.sleep(0.05)
    assert len(agent.prompts) == 1
    frames += await drain(sub)
    assert any(f.type == "rate-limited" for f in frames)
    assert any(f.type == "run-finished" and f.reason == "rate_limited" for f in frames)
    assert not any(f.type == "message" and f.message.kind == "error" for f in frames)
    await until(lambda: len(agent.prompts) == 4 and not engine.is_busy, timeout=3.0)
    assert [p.text for p in agent.prompts] == ["m1", "m1", "m2", "m3"]
    assert engine.rate_limited_until is None and await RateLimitRepo(db).get(PID) is None
    r1 = await repo.get(m1.id)
    assert r1 is not None and r1.status == "done" and r1.metadata["rateLimited"] is False


async def test_rate_limit_survives_restart(make_engine, db, until):
    await MessageRepo(db).insert_user(PID, "c1", "waiting")
    await RateLimitRepo(db).set(PID, datetime.now(UTC) + timedelta(seconds=0.3))
    engine, agent = await make_engine()
    assert engine.rate_limited_until is not None
    await asyncio.sleep(0.05)
    assert agent.prompts == []
    await until(lambda: len(agent.prompts) == 1 and not engine.is_busy, timeout=3.0)
    assert await RateLimitRepo(db).get(PID) is None


async def test_expired_rate_limit_at_start_runs_queue_immediately(make_engine, db, until):
    await MessageRepo(db).insert_user(PID, "c1", "waiting")
    await RateLimitRepo(db).set(PID, datetime.now(UTC) - timedelta(seconds=5))
    _, agent = await make_engine()
    await until(lambda: len(agent.prompts) == 1)
    assert await RateLimitRepo(db).get(PID) is None


async def test_parked_message_can_be_cancelled_and_is_not_resumed(make_engine, db, until):
    await RateLimitRepo(db).set(PID, datetime.now(UTC) + timedelta(seconds=0.2))
    engine, agent = await make_engine()
    m1, _ = await engine.submit_ex("a", "m1", [])
    assert await engine.cancel_queued(m1.id) is True
    await engine.submit_ex("b", "m2", [])
    await until(lambda: len(agent.prompts) == 1 and not engine.is_busy, timeout=3.0)
    assert [p.text for p in agent.prompts] == ["m2"]


class _RaisingAfterRateLimit:
    """A backend that reports a rejected rate limit and then fails instead of ending
    the turn with a result."""

    def __init__(self) -> None:
        self.prompts: list[QueuedItem] = []

    async def interrupt(self) -> None:
        return None

    async def start(
        self, project: Project, input_stream: AsyncIterator[QueuedItem], resume: str | None
    ) -> AsyncIterator[AgentEvent]:
        yield SessionId("s")
        async for item in input_stream:
            self.prompts.append(item)
            yield TextDelta("partial")
            yield RateLimited(resets_at=datetime.now(UTC) + timedelta(seconds=30))
            raise RuntimeError("CLI exited: rate limited")


async def test_rate_limit_followed_by_backend_failure_still_parks(make_engine, db, until):
    backend = _RaisingAfterRateLimit()
    engine, _ = await make_engine(backend=backend)
    m1, _ = await engine.submit_ex("a", "m1", [])
    await until(lambda: engine.rate_limited_until is not None and not engine.is_busy)
    row = await MessageRepo(db).get(m1.id)
    assert row is not None and row.status == "queued" and row.metadata["rateLimited"] is True
    rows, _ = await MessageRepo(db).page(PID, limit=50)
    assert not any(r.kind == "error" for r in rows)
    assert await RateLimitRepo(db).get(PID) == engine.rate_limited_until


async def test_kicad_unavailable_ends_turn_with_error_row_and_next_runs(make_engine, db, until):
    async def boom(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        raise KicadUnavailable("api-server timed out")

    spec = ToolSpec(name="boom", description="d", input_schema={"type": "object"}, handler=boom)
    engine, agent = await make_engine(
        lambda item: [Tool("boom"), Text("after")] if item.text == "m1" else [Text("two")],
        specs=[spec],
        with_session=True,
    )
    m1, _ = await engine.submit_ex("a", "m1", [])
    await engine.submit_ex("b", "m2", [])
    await until(lambda: len(agent.prompts) == 2 and not engine.is_busy)
    rows, _ = await MessageRepo(db).page(PID, limit=50)
    errors = [r for r in rows if r.kind == "error"]
    assert len(errors) == 1 and "KiCad" in errors[0].content
    # X18: the only "KiCad stopped responding" row (Phase 7 does not post another)
    assert errors[0].metadata == {"code": "kicad_unavailable"}
    assert not any(r.kind == "text" and r.content == "after" for r in rows)
    assert any(r.kind == "text" and r.content == "two" for r in rows)
    first = await MessageRepo(db).get(m1.id)
    assert first is not None and first.run_id is not None
    run = await RunRepo(db).get(first.run_id)
    assert run is not None and run["reason"] == "error"


async def test_kicad_unavailable_with_failing_backend_interrupt_posts_one_error(
    make_engine, db, until
):
    async def boom(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        raise KicadUnavailable("api-server timed out")

    spec = ToolSpec(name="boom", description="d", input_schema={"type": "object"}, handler=boom)
    engine, agent = await make_engine(
        lambda item: [Tool("boom"), Text("after")] if item.text == "m1" else [Text("two")],
        specs=[spec],
        with_session=True,
    )

    async def broken_interrupt() -> None:
        raise RuntimeError("transport gone")

    agent.interrupt = broken_interrupt
    await engine.submit_ex("a", "m1", [])
    await engine.submit_ex("b", "m2", [])
    await until(lambda: len(agent.prompts) == 2 and not engine.is_busy)
    rows, _ = await MessageRepo(db).page(PID, limit=50)
    errors = [r for r in rows if r.kind == "error"]
    assert len(errors) == 1 and errors[0].metadata == {"code": "kicad_unavailable"}
    assert any(r.kind == "text" and r.content == "two" for r in rows)


async def test_user_stop_wins_over_rate_limit_seen_earlier_in_the_turn(
    make_engine, db, until, drain
):
    gate = asyncio.Event()
    resets = datetime.now(UTC) + timedelta(seconds=0.2)

    def script(item: Any) -> list[Any]:
        if item.text == "m1":
            return [Text("partial"), Emit(RateLimited(resets_at=resets)), Wait(gate)]
        return [Text("ok")]

    engine, agent = await make_engine(script)
    sub = engine.broadcaster.subscribe(PID)
    m1, _ = await engine.submit_ex("a", "m1", [])
    await until(lambda: engine.current_run_id is not None and len(agent.prompts) == 1)
    await engine.interrupt(engine.current_run_id)
    await until(lambda: not engine.is_busy)
    frames = await drain(sub)
    assert [f.reason for f in frames if f.type == "run-finished"] == ["interrupted"]
    assert not any(f.type == "rate-limited" for f in frames)
    assert engine.rate_limited_until is None and await RateLimitRepo(db).get(PID) is None
    row = await MessageRepo(db).get(m1.id)
    assert row is not None and row.status != "queued"
    await asyncio.sleep(0.4)  # past resets_at: nothing is auto-rerun
    assert [p.text for p in agent.prompts] == ["m1"]
    await engine.submit_ex("b", "m2", [])
    await until(lambda: len(agent.prompts) == 2 and not engine.is_busy)
