import asyncio
from collections.abc import AsyncIterator
from typing import Any

from copperchat.agent.engine import RunHooks, TurnInfo, TurnOutcome
from copperchat.agent.events import AgentEvent, QueuedItem, TextDelta, TurnFinished, Usage
from copperchat.agent.fake import Emit, Text, Think, Tool, Wait
from copperchat.db.repos.agent_state import RunRepo
from copperchat.db.repos.messages import MessageRepo
from copperchat.projects import Project, agent_dir
from copperchat.tools import ImageBlock, ToolContext, ToolResult, ToolSpec

PID = "a1b2c3d4e5f6"


async def _rows(db):  # type: ignore[no-untyped-def]
    rows, _ = await MessageRepo(db).page(PID, limit=100)
    return rows


async def test_submit_persists_queued_first_then_streams_and_finishes(
    make_engine, db, until, drain
):
    engine, agent = await make_engine()
    sub = engine.broadcaster.subscribe(PID)
    dto, created = await engine.submit_ex("c1", "hi", [])
    assert created and dto.status == "queued" and dto.client_message_id == "c1"
    await until(lambda: len(agent.prompts) == 1 and not engine.is_busy)
    rows = await _rows(db)
    assert [(r.role, r.kind, r.status) for r in rows] == [
        ("user", "text", "done"),
        ("assistant", "text", "done"),
    ]
    assert rows[1].content == "You said: hi" and rows[0].run_id == rows[1].run_id
    frames = await drain(sub)
    types = [f.type for f in frames]
    assert types[0] == "message" and frames[0].message.status == "queued"
    assert types.index("run-started") < types.index("message-delta", types.index("run-started"))
    assert types[-1] == "run-finished" and frames[-1].reason == "completed"
    assert "usage" in types
    assert all(f.project_id == PID for f in frames)
    run = await RunRepo(db).get(frames[-1].run_id)
    assert run is not None and run["reason"] == "completed"


async def test_retransmitted_client_message_id_is_one_turn(make_engine, db, until):
    gate = asyncio.Event()
    engine, agent = await make_engine(lambda item: [Wait(gate), Text("ok")])
    results = await asyncio.gather(*(engine.submit_ex("same", "hello", []) for _ in range(3)))
    assert sorted(created for _, created in results) == [False, False, True]
    assert len({dto.id for dto, _ in results}) == 1
    gate.set()
    await until(lambda: not engine.is_busy)
    await asyncio.sleep(0.05)
    assert [p.client_message_id for p in agent.prompts] == ["same"]
    assert len([r for r in await _rows(db) if r.role == "user"]) == 1


async def test_next_message_is_released_only_after_turn_finished(make_engine, db, until):
    gate = asyncio.Event()
    engine, agent = await make_engine(
        lambda item: [Wait(gate), Text("one")] if item.text == "m1" else [Text("two")]
    )
    _m1, _ = await engine.submit_ex("a", "m1", [])
    m2, _ = await engine.submit_ex("b", "m2", [])
    await until(lambda: len(agent.prompts) == 1)
    await asyncio.sleep(0.05)
    assert len(agent.prompts) == 1, "m2 must not reach the backend while m1 runs"
    row2 = await MessageRepo(db).get(m2.id)
    assert row2 is not None and row2.status == "queued"
    gate.set()
    await until(lambda: len(agent.prompts) == 2 and not engine.is_busy)
    assert agent.resume_ids == [None], "both turns ran in one burst"
    users = [r for r in await _rows(db) if r.role == "user"]
    assert users[0].run_id != users[1].run_id


async def test_streamed_text_is_persisted_by_size_and_by_time(make_engine, db, until):
    gate = asyncio.Event()
    engine, _ = await make_engine(
        lambda item: [Text("a" * 3000), Text("b"), Wait(gate)], flush_interval_s=0.05
    )
    await engine.submit_ex("c", "go", [])
    repo = MessageRepo(db)

    async def assistant_content() -> str:
        rows = [r for r in await _rows(db) if r.role == "assistant"]
        return rows[0].content if rows else ""

    await until(lambda: engine.current_run_id is not None)
    await asyncio.sleep(0.2)
    assert await assistant_content() == "a" * 3000 + "b"
    [row] = [r for r in await _rows(db) if r.role == "assistant"]
    assert row.status == "streaming"
    assert engine.to_dto(row).content == "a" * 3000 + "b"
    gate.set()
    await until(lambda: not engine.is_busy)
    done = await repo.get(row.id)
    assert done is not None and done.status == "done"


async def test_thinking_tool_use_result_and_image_are_separate_rows(
    make_engine, db, until, project
):
    async def probe(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        return ToolResult(data={"ok": 1}, images=[ImageBlock(mime="image/png", data=b"PNG")])

    spec = ToolSpec(name="probe", description="d", input_schema={"type": "object"}, handler=probe)
    engine, _ = await make_engine(
        lambda item: [Think("hmm"), Text("x"), Tool("probe", {"a": 1}), Text("y")],
        specs=[spec],
        with_session=True,
    )
    await engine.submit_ex("c", "go", [])
    await until(lambda: not engine.is_busy and engine.current_run_id is None)
    await asyncio.sleep(0.05)
    rows = await _rows(db)
    assert [r.kind for r in rows] == [
        "text",
        "thinking",
        "text",
        "tool_use",
        "tool_result",
        "image",
        "text",
    ]
    tool_use, tool_result, image = rows[3], rows[4], rows[5]
    assert tool_use.content == "probe" and tool_use.metadata["input"] == {"a": 1}
    assert tool_result.metadata["toolUseId"] == tool_use.metadata["toolUseId"]
    url = image.metadata["url"]
    assert url.startswith(f"/api/projects/{PID}/images/")
    assert (agent_dir(project) / "images" / url.rsplit("/", 1)[1]).read_bytes() == b"PNG"
    assert tool_result.metadata["images"] == [{"url": url}]


async def test_hooks_and_agent_notes(make_engine, until):
    calls: list[Any] = []

    class Rec(RunHooks):
        async def on_engine_start(self) -> None:
            calls.append("engine")

        async def on_turn_start(self, turn: TurnInfo) -> None:
            calls.append(("start", turn.text))

        async def on_turn_end(self, turn: TurnInfo, outcome: TurnOutcome) -> None:
            calls.append(("end", outcome.reason))

    engine, agent = await make_engine(hooks=Rec())
    engine.add_agent_note("history restored")
    await engine.submit_ex("c", "hi", [])
    await until(lambda: ("end", "completed") in calls)
    assert calls == ["engine", ("start", "hi"), ("end", "completed")]
    assert agent.prompts[0].text == "[CopperChat notice] history restored\n\nhi"


# ---- additions beyond the brief


async def test_changed_docs_are_recorded_in_outcome_and_not_published(make_engine, until, drain):
    """X2: Phase 5 never publishes view-updated; it only records changed_docs."""
    outcomes: list[TurnOutcome] = []

    class Rec(RunHooks):
        async def on_turn_end(self, turn: TurnInfo, outcome: TurnOutcome) -> None:
            outcomes.append(outcome)

    async def edit(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        return ToolResult(data={}, changed_docs={"sch"})

    spec = ToolSpec(name="edit", description="d", input_schema={"type": "object"}, handler=edit)
    engine, _ = await make_engine(
        lambda item: [Tool("edit")], specs=[spec], with_session=True, hooks=Rec()
    )
    sub = engine.broadcaster.subscribe(PID)
    await engine.submit_ex("c", "go", [])
    await until(lambda: len(outcomes) == 1)
    assert outcomes[0].changed_docs == frozenset({"sch"})
    assert "view-updated" not in [f.type for f in await drain(sub)]


async def test_unsolicited_turn_finished_neither_finishes_nor_releases(make_engine, db, until):
    """A ResultMessage of a background task-notification turn (X20 (3)) is tagged
    unsolicited by the adapter: it must not end the running turn."""
    gate = asyncio.Event()
    stray = TurnFinished(reason="completed", unsolicited=True)
    engine, agent = await make_engine(
        lambda item: (
            [Text("a"), Emit(stray), Wait(gate), Text("b")] if item.text == "m1" else [Text("two")]
        )
    )
    await engine.submit_ex("a", "m1", [])
    await engine.submit_ex("b", "m2", [])
    await until(lambda: engine.current_run_id is not None)
    run1 = engine.current_run_id
    await asyncio.sleep(0.05)
    assert engine.current_run_id == run1, "stray result must not end the running turn"
    assert len(agent.prompts) == 1
    assert run1 is not None
    run = await RunRepo(db).get(run1)
    assert run is not None and run["finished_at"] is None
    gate.set()
    await until(lambda: len(agent.prompts) == 2 and not engine.is_busy)
    assistant = [r.content for r in await _rows(db) if r.role == "assistant"]
    assert assistant == ["ab", "two"]


class _EagerBackend:
    """Pulls input like the SDK does (in its own task, as soon as the gate opens) and
    emits whatever the test scripts into `events`."""

    def __init__(self) -> None:
        self.pulled: list[int] = []
        self.events: asyncio.Queue[AgentEvent | None] = asyncio.Queue()

    async def interrupt(self) -> None:
        return None

    async def start(
        self, project: Project, input_stream: AsyncIterator[QueuedItem], resume: str | None
    ) -> AsyncIterator[AgentEvent]:
        async def pump() -> None:
            async for item in input_stream:
                self.pulled.append(item.message_id)

        task = asyncio.create_task(pump())
        try:
            while (ev := await self.events.get()) is not None:
                yield ev
        finally:
            task.cancel()


def _usage() -> Usage:
    return Usage(input_tokens=1, output_tokens=1, cache_read_tokens=0, cache_creation_tokens=0)


async def test_stray_results_never_release_the_next_item_early(make_engine, until):
    backend = _EagerBackend()
    engine, _ = await make_engine(backend=backend)
    m1, _ = await engine.submit_ex("a", "m1", [])
    m2, _ = await engine.submit_ex("b", "m2", [])
    m3, _ = await engine.submit_ex("c", "m3", [])
    await until(lambda: backend.pulled == [m1.id])
    run1 = engine.current_run_id
    # A task-notification result mid-turn: no release, same turn.
    await backend.events.put(TextDelta("x"))
    await backend.events.put(TurnFinished(reason="completed", unsolicited=True))
    await asyncio.sleep(0.05)
    assert backend.pulled == [m1.id] and engine.current_run_id == run1
    # The real result releases exactly one item.
    await backend.events.put(_usage())
    await backend.events.put(TurnFinished(reason="completed"))
    await until(lambda: backend.pulled == [m1.id, m2.id])
    await until(lambda: engine.current_run_id not in (None, run1))
    run2 = engine.current_run_id
    # Another stray result while m2 runs must not let m3 through nor end m2.
    await backend.events.put(TurnFinished(reason="completed", unsolicited=True))
    await asyncio.sleep(0.05)
    assert backend.pulled == [m1.id, m2.id] and engine.current_run_id == run2
    await backend.events.put(TurnFinished(reason="completed"))
    await until(lambda: backend.pulled == [m1.id, m2.id, m3.id])
    await backend.events.put(TurnFinished(reason="completed"))
    await until(lambda: engine.current_run_id is None)
    await backend.events.put(None)
    await until(lambda: not engine.is_busy)


async def test_result_outside_a_turn_is_ignored_and_bursts_do_not_overlap(make_engine, until):
    """An extra result with no item in flight is ignored; a message sent while the
    previous burst is still winding down starts its burst only after that one ended."""
    backend = _EagerBackend()
    engine, _ = await make_engine(backend=backend)
    m1, _ = await engine.submit_ex("a", "m1", [])
    await until(lambda: backend.pulled == [m1.id])
    await backend.events.put(TurnFinished(reason="completed"))
    await until(lambda: engine.current_run_id is None)
    await backend.events.put(TurnFinished(reason="completed"))  # duplicate, nothing in flight
    await asyncio.sleep(0.05)
    assert engine.current_run_id is None and engine.is_busy  # old burst still winding down
    m2, created = await engine.submit_ex("b", "m2", [])
    assert created
    await asyncio.sleep(0.05)
    assert backend.pulled == [m1.id], "new burst must wait for the previous one"
    await backend.events.put(None)  # old burst's backend iterator ends
    await until(lambda: backend.pulled == [m1.id, m2.id])
    await until(lambda: engine.current_run_id is not None)
    await backend.events.put(TurnFinished(reason="completed"))
    await until(lambda: engine.current_run_id is None)
    await backend.events.put(None)
    await until(lambda: not engine.is_busy)


# ---- fix round 1


async def test_burst_end_racing_a_submit_does_not_double_queue(make_engine, until):
    """A burst's wind-down re-drive (`_drive_queued`) landing while a submit's insert has
    just committed must not enqueue the same message a second time."""
    engine, agent = await make_engine()
    repo = engine._messages
    original = repo.insert

    async def insert_then_redrive(*args: Any, **kw: Any) -> int:
        mid = await original(*args, **kw)
        task = asyncio.ensure_future(engine._drive_queued())
        await asyncio.sleep(0.01)  # let the re-drive scan the committed 'queued' row
        await task
        return mid

    repo.insert = insert_then_redrive  # type: ignore[method-assign]
    await engine.submit_ex("c1", "hi", [])
    repo.insert = original  # type: ignore[method-assign]
    await until(lambda: not engine.is_busy)
    await asyncio.sleep(0.05)
    assert [p.client_message_id for p in agent.prompts] == ["c1"]


class _TrackingBackend(_EagerBackend):
    def __init__(self) -> None:
        super().__init__()
        self.started = 0
        self.ended = 0

    async def start(
        self, project: Project, input_stream: AsyncIterator[QueuedItem], resume: str | None
    ) -> AsyncIterator[AgentEvent]:
        self.started += 1
        try:
            async for ev in super().start(project, input_stream, resume):
                yield ev
        finally:
            self.ended += 1


async def test_close_during_wind_down_stops_every_burst(make_engine, until):
    backend = _TrackingBackend()
    engine, _ = await make_engine(backend=backend)
    m1, _ = await engine.submit_ex("a", "m1", [])
    await until(lambda: backend.pulled == [m1.id])
    old = engine._burst
    await backend.events.put(TurnFinished(reason="completed"))
    await until(lambda: engine.current_run_id is None)
    await engine.submit_ex("b", "m2", [])  # new burst waits for the winding-down one
    new = engine._burst
    assert old is not None and new is not None and old is not new
    await engine.close()
    assert old.done() and new.done()
    assert backend.started == backend.ended == 1


async def test_wind_down_wait_is_bounded(make_engine, until):
    """A previous burst whose backend never returns is cancelled after the grace period
    so later messages are not stuck in the queue."""
    backend = _TrackingBackend()
    engine, _ = await make_engine(backend=backend, interrupt_grace_s=0.1)
    m1, _ = await engine.submit_ex("a", "m1", [])
    await until(lambda: backend.pulled == [m1.id])
    await backend.events.put(TurnFinished(reason="completed"))
    await until(lambda: engine.current_run_id is None)
    m2, _ = await engine.submit_ex("b", "m2", [])
    await until(lambda: backend.pulled == [m1.id, m2.id])
    assert backend.started == 2 and backend.ended == 1
    await backend.events.put(TurnFinished(reason="completed"))
    await until(lambda: engine.current_run_id is None)
    await backend.events.put(None)
    await until(lambda: not engine.is_busy)


async def test_failing_hooks_do_not_block_the_queue(make_engine, db, until):
    class Boom(RunHooks):
        async def on_engine_start(self) -> None:
            raise RuntimeError("engine hook")

        async def on_turn_start(self, turn: TurnInfo) -> None:
            raise RuntimeError("start hook")

        async def on_turn_end(self, turn: TurnInfo, outcome: TurnOutcome) -> None:
            raise RuntimeError("end hook")

    engine, agent = await make_engine(hooks=Boom())
    await engine.submit_ex("a", "m1", [])
    await engine.submit_ex("b", "m2", [])
    await until(lambda: len(agent.prompts) == 2 and not engine.is_busy)
    users = [r for r in await _rows(db) if r.role == "user"]
    for u in users:
        run = await RunRepo(db).get(u.run_id)
        assert run is not None and run["reason"] == "completed"


async def test_queued_rows_are_redriven_even_if_engine_start_hook_fails(make_engine, db, until):
    class Boom(RunHooks):
        async def on_engine_start(self) -> None:
            raise RuntimeError("engine hook")

    await MessageRepo(db).insert_user(PID, "old", "from before the restart")
    engine, agent = await make_engine(hooks=Boom())
    await until(lambda: len(agent.prompts) == 1 and not engine.is_busy)


async def test_size_trigger_flushes_without_waiting_for_the_timer(make_engine, db, until):
    gate = asyncio.Event()
    engine, _ = await make_engine(
        lambda item: [Text("a" * 3000), Wait(gate)], flush_interval_s=60.0
    )
    await engine.submit_ex("c", "go", [])

    async def persisted() -> str:
        rows = [r for r in await _rows(db) if r.role == "assistant"]
        return rows[0].content if rows else ""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    while await persisted() != "a" * 3000:
        assert loop.time() < deadline, "2048-char rule did not flush"
        await asyncio.sleep(0.01)
    gate.set()
    await until(lambda: not engine.is_busy)
