import asyncio

from copperchat.agent.fake import Text, Wait
from copperchat.db.repos.agent_state import RunRepo
from copperchat.db.repos.messages import MessageRepo

PID = "a1b2c3d4e5f6"


async def test_cancel_queued_before_pull(make_engine, db, until, drain):
    gate = asyncio.Event()
    engine, agent = await make_engine(lambda item: [Wait(gate), Text("x")])
    await engine.submit_ex("a", "m1", [])
    m2, _ = await engine.submit_ex("b", "m2", [])
    await until(lambda: len(agent.prompts) == 1)
    sub = engine.broadcaster.subscribe(PID)
    assert await engine.cancel_queued(m2.id) is True
    frames = await drain(sub)
    assert any(f.type == "message-delta" and f.status_patch == "cancelled" for f in frames)
    gate.set()
    await until(lambda: not engine.is_busy)
    await asyncio.sleep(0.05)
    assert [p.text for p in agent.prompts] == ["m1"]
    row = await MessageRepo(db).get(m2.id)
    assert row is not None and row.status == "cancelled"


async def test_cancel_after_pull_returns_false_and_message_proceeds(make_engine, db, until):
    gate = asyncio.Event()
    engine, agent = await make_engine(lambda item: [Wait(gate), Text("x")])
    m1, _ = await engine.submit_ex("a", "m1", [])
    await until(lambda: len(agent.prompts) == 1)
    assert await engine.cancel_queued(m1.id) is False
    gate.set()
    await until(lambda: not engine.is_busy)
    rows, _ = await MessageRepo(db).page(PID, limit=10)
    assert rows[0].status == "done" and rows[-1].content == "x"


async def test_cancel_queued_rejects_unknown_and_non_user_rows(make_engine, db, until):
    engine, agent = await make_engine()
    assert await engine.cancel_queued(999_999) is False
    await engine.submit_ex("a", "m1", [])
    await until(lambda: len(agent.prompts) == 1 and not engine.is_busy)
    rows, _ = await MessageRepo(db).page(PID, limit=10)
    reply = next(r for r in rows if r.role == "assistant")
    assert await engine.cancel_queued(reply.id) is False
    assert (await MessageRepo(db).get(reply.id)).status == "done"  # type: ignore[union-attr]


async def test_interrupt_cancels_queue_closes_stream_and_ends_run(make_engine, db, until):
    never = asyncio.Event()
    engine, agent = await make_engine(lambda item: [Text("a"), Wait(never)])
    await engine.submit_ex("a", "m1", [])
    m2, _ = await engine.submit_ex("b", "m2", [])
    await until(lambda: engine.current_run_id is not None)
    run_id = engine.current_run_id
    await engine.interrupt(run_id)
    await until(lambda: not engine.is_busy)
    assert agent.interrupts == 1
    run = await RunRepo(db).get(run_id)  # type: ignore[arg-type]
    assert run is not None and run["reason"] == "interrupted"
    row = await MessageRepo(db).get(m2.id)
    assert row is not None and row.status == "cancelled"
    assert [p.text for p in agent.prompts] == ["m1"]


async def test_stuck_stop_backstop_cancels_burst(make_engine, db, until, drain):
    never = asyncio.Event()
    engine, agent = await make_engine(
        lambda item: [Wait(never)], ignore_interrupt=True, interrupt_grace_s=0.1
    )
    await engine.submit_ex("a", "m1", [])
    await until(lambda: engine.current_run_id is not None)
    run_id = engine.current_run_id
    sub = engine.broadcaster.subscribe(PID)
    await engine.interrupt(None)
    await asyncio.sleep(0.05)
    assert engine.is_busy, "backend ignores interrupt; the backstop has not fired yet"
    await until(lambda: not engine.is_busy, timeout=2.0)
    frames = await drain(sub)
    assert any(f.type == "run-finished" and f.reason == "interrupted" for f in frames)
    run = await RunRepo(db).get(run_id)  # type: ignore[arg-type]
    assert run is not None and run["reason"] == "interrupted"
    # the engine is usable again afterwards (a fresh burst pulls the next message)
    await engine.submit_ex("b", "m2", [])
    await until(lambda: len(agent.prompts) == 2)


async def test_backstop_spares_a_message_sent_after_the_interrupt(make_engine, db, until):
    """The backstop targets the interrupted turn, not the burst: a message submitted
    after Stop (it reopens the burst's stream) must not be cancelled by it."""
    never, gate = asyncio.Event(), asyncio.Event()
    engine, agent = await make_engine(
        lambda item: [Text("a"), Wait(never)] if item.text == "m1" else [Wait(gate), Text("ok")],
        interrupt_grace_s=0.1,
    )
    await engine.submit_ex("a", "m1", [])
    await until(lambda: engine.current_run_id is not None)
    await engine.interrupt(engine.current_run_id)
    m2, _ = await engine.submit_ex("b", "m2", [])
    await until(lambda: len(agent.prompts) == 2 and engine.current_run_id is not None)
    await asyncio.sleep(0.3)  # well past the grace period
    assert engine.is_busy
    gate.set()
    await until(lambda: not engine.is_busy)
    row = await MessageRepo(db).get(m2.id)
    assert row is not None and row.run_id is not None
    run = await RunRepo(db).get(row.run_id)
    assert run is not None and run["reason"] == "completed"


async def test_interrupt_for_other_run_is_ignored(make_engine, until):
    gate = asyncio.Event()
    engine, agent = await make_engine(lambda item: [Wait(gate)])
    await engine.submit_ex("a", "m1", [])
    await until(lambda: engine.current_run_id is not None)
    await engine.interrupt("some-old-run")
    assert agent.interrupts == 0
    gate.set()


async def test_interrupt_when_idle_is_a_no_op(make_engine, until):
    engine, agent = await make_engine()
    await engine.interrupt(None)
    assert agent.interrupts == 0 and not engine.is_busy
    await engine.submit_ex("a", "m1", [])
    await until(lambda: len(agent.prompts) == 1 and not engine.is_busy)
