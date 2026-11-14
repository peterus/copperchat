"""Near-live schematic view: debounce/throttle, lock ordering, finish/close, failures."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from copperchat.agent.protocol import ViewUpdatedFrame
from copperchat.history.live import LiveViewPublisher
from copperchat.kicad_session import DocKind, KicadSession
from copperchat.projects import Project
from copperchat.views import write_kicanvas_snapshot
from copperchat.views.snapshot import snapshot_path
from tests.unit.history.conftest import Opened
from tests.unit.history.test_history_hooks import done, turn


async def settle() -> None:
    for _ in range(50):
        await asyncio.sleep(0)


class FakeTime:
    """Monotonic clock + sleep that only advance when the test says so."""

    def __init__(self) -> None:
        self.now = 0.0
        self.waiters: list[tuple[float, asyncio.Future[None]]] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waiters.append((self.now + delay, fut))
        await fut

    async def advance(self, dt: float) -> None:
        """Move the clock by `dt`, waking every sleeper at its own deadline in order."""
        target = self.now + dt
        while True:
            await settle()
            self.waiters = [w for w in self.waiters if not w[1].done()]
            due = [w for w in self.waiters if w[0] <= target]
            if not due:
                break
            at, fut = min(due, key=lambda w: w[0])
            self.now = max(self.now, at)
            fut.set_result(None)
        self.now = target
        await settle()


@dataclass
class Rig:
    opened: Opened
    time: FakeTime
    live: LiveViewPublisher
    calls: list[tuple[float, set[DocKind], bool]] = field(default_factory=list)
    gate: asyncio.Event | None = None  # set: publish_view waits for it
    error: Exception | None = None

    @property
    def times(self) -> list[float]:
        return [t for t, _, _ in self.calls]

    def schedule(self) -> None:
        self.live.schedule(self.opened.project)


def make_rig(opened: Opened, **kw: Any) -> Rig:
    t = FakeTime()
    svc = opened.env.svc
    live = LiveViewPublisher(svc, clock=t.clock, sleep=t.sleep, **kw)
    svc.live = live
    rig = Rig(opened, t, live)
    real: Callable[..., Awaitable[int]] = svc.publish_view

    async def recording(
        project: Project, session: KicadSession, docs: set[DocKind], *, live: bool = False
    ) -> int:
        rig.calls.append((t.now, set(docs), live))
        if rig.gate is not None:
            await rig.gate.wait()
        if rig.error is not None:
            raise rig.error
        if not live:
            return await real(project, session, docs, live=live)
        # No real live write: its hashing thread finishes in wall-clock time, so the
        # fake-clock time of the publish's end would depend on the machine's load.
        return session.view_version

    svc.publish_view = recording  # type: ignore[method-assign]
    return rig


@pytest.fixture
def rig(opened: Opened) -> Rig:
    return make_rig(opened)


# ---- debounce / throttle


async def test_single_change_publishes_once_after_the_debounce(rig: Rig) -> None:
    rig.schedule()
    await rig.time.advance(1.9)
    assert rig.calls == []
    await rig.time.advance(0.1)
    assert rig.calls == [(2.0, {"sch"}, True)]
    await rig.time.advance(30)
    assert len(rig.calls) == 1


async def test_burst_of_changes_publishes_once(rig: Rig) -> None:
    for _ in range(4):
        rig.schedule()
        await rig.time.advance(0.5)
    await rig.time.advance(30)
    assert rig.times == [3.5]  # last change at 1.5 + debounce


async def test_continuous_changes_still_publish_by_max_wait(rig: Rig) -> None:
    for _ in range(10):  # a change every second, faster than the debounce
        rig.schedule()
        await rig.time.advance(1.0)
    assert rig.times[0] == 6.0  # first change at 0 + max wait
    await rig.time.advance(30)
    assert rig.times == [6.0, 11.0]  # last change at 9 + debounce


async def test_min_interval_between_two_live_publishes(opened: Opened) -> None:
    rig = make_rig(opened, debounce_s=0.5, min_interval_s=2.5)
    rig.schedule()
    await rig.time.advance(0.6)
    assert rig.times == [0.5]
    rig.schedule()  # debounce alone would publish at 1.1
    await rig.time.advance(30)
    assert rig.times == [0.5, 3.0]


async def test_change_during_publish_gets_exactly_one_follow_up(rig: Rig) -> None:
    rig.gate = asyncio.Event()
    rig.schedule()
    await rig.time.advance(2.0)
    assert rig.times == [2.0]
    rig.schedule()
    await rig.time.advance(0.2)
    rig.schedule()
    rig.gate.set()
    await rig.time.advance(30)
    assert rig.times == [2.0, 4.7]  # publish ended at 2.2, + min interval (> + debounce)


# ---- lock ordering, finish, close


async def test_live_publish_waits_for_the_history_lock(rig: Rig) -> None:
    svc, project = rig.opened.env.svc, rig.opened.project
    async with svc.lock(project):  # e.g. a restore or the turn-end autosave
        rig.schedule()
        await rig.time.advance(5)
        assert rig.calls == []
    await settle()
    assert rig.times == [5.0]


async def test_finish_while_sleeping_cancels_without_publishing(rig: Rig) -> None:
    rig.schedule()
    await rig.time.advance(1.0)
    await rig.live.finish(rig.opened.project)
    await rig.time.advance(30)
    assert rig.calls == []


async def test_finish_while_publishing_awaits_it_and_never_loops(rig: Rig) -> None:
    rig.gate = asyncio.Event()
    rig.schedule()
    await rig.time.advance(2.0)
    rig.schedule()  # pending change of the same turn: the turn end publishes it
    finishing = asyncio.create_task(rig.live.finish(rig.opened.project))
    await settle()
    assert not finishing.done()  # never cancelled mid-publish
    rig.gate.set()
    await finishing
    await rig.time.advance(30)
    assert rig.times == [2.0]


async def test_close_cancels_even_a_running_publish(rig: Rig) -> None:
    rig.gate = asyncio.Event()  # never set
    rig.schedule()
    await rig.time.advance(2.0)
    await asyncio.wait_for(rig.live.close(rig.opened.project), 5)
    rig.schedule()
    await rig.live.close(rig.opened.project)
    await rig.time.advance(30)
    assert rig.times == [2.0]


async def test_cancelled_finish_cancels_the_running_publish(rig: Rig) -> None:
    """finish popped the state: if its caller is cancelled while awaiting the publish,
    nothing else could ever cancel that task - finish cancels it itself."""
    rig.gate = asyncio.Event()  # never set
    rig.schedule()
    await rig.time.advance(2.0)
    task = rig.live._states[rig.opened.project.id].task
    assert task is not None and not task.done()
    finishing = asyncio.create_task(rig.live.finish(rig.opened.project))
    await settle()
    finishing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await finishing
    await settle()
    assert task.cancelled()


async def test_finish_and_close_without_state_are_no_ops(rig: Rig) -> None:
    await rig.live.finish(rig.opened.project)
    await rig.live.close(rig.opened.project)


# ---- failures and skips


async def test_publish_failure_only_logs_and_next_schedule_works(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.error = RuntimeError("SaveCopy failed")
    rig.schedule()
    with caplog.at_level(logging.WARNING, logger="copperchat.history.live"):
        await rig.time.advance(5)
    assert len(rig.calls) == 1 and "live view" in caplog.text
    rig.error = None
    rig.schedule()
    await rig.time.advance(5)
    assert len(rig.calls) == 2


async def test_read_only_or_closed_session_never_publishes(rig: Rig) -> None:
    rig.opened.session.read_only = True
    rig.schedule()
    await rig.time.advance(5)
    rig.opened.session.read_only = False
    await rig.opened.env.sessions.close_all()
    rig.schedule()
    await rig.time.advance(5)
    assert rig.calls == []


async def test_session_replaced_while_waiting_for_the_lock_skips(rig: Rig) -> None:
    env, project = rig.opened.env, rig.opened.project
    async with env.svc.lock(project):
        rig.schedule()
        await rig.time.advance(5)
        await env.sessions.reopen(project)  # e.g. a restore
    await settle()
    assert rig.calls == []


# ---- HistoryHooks wiring


async def test_hooks_schedule_only_schematic_changes(rig: Rig) -> None:
    hooks = rig.opened.hooks
    hooks.on_documents_changed(turn(), {"pcb"})
    await rig.time.advance(10)
    assert rig.calls == []
    hooks.on_documents_changed(turn(), {"sch", "pcb"})
    await rig.time.advance(10)
    assert rig.calls == [(2.0 + 10, {"sch"}, True)]


async def test_turn_end_finishes_the_live_publisher_before_the_autosave(rig: Rig) -> None:
    order: list[str] = []
    svc = rig.opened.env.svc
    real_finish, real_publish = rig.live.finish, svc.publish_view

    async def finish(project: Project) -> None:
        order.append("finish")
        await real_finish(project)

    async def publish(project: Project, session: KicadSession, docs: Any, **kw: Any) -> int:
        order.append(f"publish:{kw.get('live', False)}")
        return await real_publish(project, session, docs, **kw)

    rig.live.finish = finish  # type: ignore[method-assign]
    svc.publish_view = publish  # type: ignore[method-assign]
    rig.opened.hooks.on_documents_changed(turn(), {"sch"})
    await rig.time.advance(1.0)
    await rig.opened.hooks.on_turn_end(turn(), done("sch", reason="kicad_failed", failed=True))
    assert order == ["finish"]  # every outcome finishes; nothing live after it
    rig.opened.hooks.on_documents_changed(turn(), {"sch"})
    await rig.time.advance(1.0)
    await rig.opened.hooks.on_turn_end(turn(), done("sch"))
    assert order == ["finish", "finish", "publish:False"]
    await rig.time.advance(30)
    assert order == ["finish", "finish", "publish:False"]


async def test_engine_close_hook_closes_the_publisher(rig: Rig) -> None:
    rig.opened.hooks.on_documents_changed(turn(), {"sch"})
    await rig.opened.hooks.on_engine_close()
    await rig.time.advance(30)
    assert rig.calls == []


# ---- the real publish_view behind the publisher


def view_frames(opened: Opened) -> list[tuple[int, list[str] | None, bool | None]]:
    return [
        (f.version, f.changed_files, f.live)
        for f in opened.env.broadcaster.frames
        if isinstance(f, ViewUpdatedFrame)
    ]


async def until(cond: Callable[[], bool]) -> None:
    """publish_view hashes in a thread: wait for it in real time."""
    for _ in range(500):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


def real_rig(opened: Opened) -> tuple[FakeTime, LiveViewPublisher]:
    t = FakeTime()
    live = LiveViewPublisher(opened.env.svc, clock=t.clock, sleep=t.sleep)
    opened.env.svc.live = live
    return t, live


async def test_live_publish_then_turn_end_through_the_real_publish_view(opened: Opened) -> None:
    project, svc = opened.project, opened.env.svc
    content = {"sch": b"(kicad_sch v1)"}

    async def write_view(session: KicadSession, doc: DocKind, *, save_modified: bool = True) -> Any:
        path = snapshot_path(project, doc)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.get(doc, b"(board)"))
        return path

    svc.write_view = write_view  # type: ignore[assignment]
    t, live = real_rig(opened)
    root = snapshot_path(project, "sch").name
    await svc.publish_view(project, opened.session, {"sch"})  # what the clients have
    content["sch"] = b"(kicad_sch v2)"
    opened.hooks.on_documents_changed(turn(), {"sch"})
    await t.advance(2.0)
    await until(lambda: len(view_frames(opened)) == 2)
    assert view_frames(opened)[1] == (2, [root], True)
    await until(lambda: not live._states[project.id].publishing)
    await opened.hooks.on_turn_end(turn(), done("sch"))
    assert view_frames(opened)[2:] == [(3, [], None)]  # nothing new since the live one


async def test_live_publish_of_a_modified_schematic_is_skipped(
    opened: Opened, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed partial edit (document modified): no save, no frame, no version bump."""
    env, project, session = opened.env, opened.project, opened.session
    env.svc.write_view = write_kicanvas_snapshot
    t, live = real_rig(opened)
    env.server.modified = True
    with caplog.at_level(logging.INFO, logger="copperchat.history.live"):
        live.schedule(project)
        task = live._states[project.id].task
        assert task is not None
        await t.advance(2.0)
        await until(task.done)
    assert "skipped" in caplog.text
    assert env.server.saved == [] and env.server.modified is True
    assert view_frames(opened) == [] and session.view_version == 0
    assert not snapshot_path(project, "sch").exists()
