"""Near-live schematic view: debounce/throttle, lock ordering, finish/close, failures."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from copperchat.agent.engine import TurnOutcome
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
    disk: int = 0  # publish_disk_view calls (also recorded in `calls` as ({"sch"}, False))
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
        project: Project,
        session: KicadSession,
        docs: set[DocKind],
        *,
        live: bool = False,
        **kw: Any,
    ) -> int:
        rig.calls.append((t.now, set(docs), live))
        if rig.gate is not None:
            await rig.gate.wait()
        if rig.error is not None:
            raise rig.error
        if not live:
            return await real(project, session, docs, live=live, **kw)
        # No real live write: its hashing thread finishes in wall-clock time, so the
        # fake-clock time of the publish's end would depend on the machine's load.
        session.view_version += 1  # a live frame was sent
        return session.view_version

    real_disk: Callable[[Project], Awaitable[int | None]] = svc.publish_disk_view

    async def recording_disk(project: Project) -> int | None:
        rig.calls.append((t.now, {"sch"}, False))
        rig.disk += 1
        if rig.error is not None:
            raise rig.error
        return await real_disk(project)

    svc.publish_view = recording  # type: ignore[method-assign]
    svc.publish_disk_view = recording_disk  # type: ignore[method-assign]
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


# ---- the turn end always catches up after live frames (non-live schematic frame)


async def live_frame_sent(rig: Rig) -> None:
    rig.opened.hooks.on_documents_changed(turn(), {"sch"})
    await rig.time.advance(2.0)
    assert rig.calls == [(2.0, {"sch"}, True)]


def turn_end_calls(rig: Rig) -> list[tuple[set[DocKind], bool]]:
    return [(docs, live) for _, docs, live in rig.calls[1:]]


async def test_finish_reports_whether_a_live_frame_was_sent(rig: Rig) -> None:
    project = rig.opened.project
    rig.schedule()
    await rig.time.advance(1.0)
    assert await rig.live.finish(project) is False  # cancelled before publishing
    rig.schedule()
    await rig.time.advance(2.0)
    assert await rig.live.finish(project) is True


async def test_unchanged_live_publish_counts_as_not_sent(rig: Rig) -> None:
    real = rig.opened.env.svc.publish_view

    async def unchanged(project: Project, session: KicadSession, docs: Any, **kw: Any) -> int:
        if kw.get("live"):
            return session.view_version  # nothing changed: no frame, no bump
        return await real(project, session, docs, **kw)

    rig.opened.env.svc.publish_view = unchanged  # type: ignore[method-assign]
    rig.schedule()
    await rig.time.advance(2.0)
    assert await rig.live.finish(rig.opened.project) is False


async def test_success_after_live_frames_sends_exactly_one_turn_end_frame(rig: Rig) -> None:
    await live_frame_sent(rig)
    await rig.opened.hooks.on_turn_end(turn(), done("sch"))
    assert turn_end_calls(rig) == [({"sch"}, False)]


async def test_board_only_save_after_live_frames_carries_the_schematic(rig: Rig) -> None:
    await live_frame_sent(rig)
    rig.opened.session.modified_docs.add("sch")
    rig.opened.env.save_error, rig.opened.env.save_error_doc = RuntimeError("full"), "sch"
    await rig.opened.hooks.on_turn_end(turn(), done("pcb"))  # e.g. sch edits saved, then pcb
    # pcb saved, sch holds unsaved edits: one frame for the board, one of the
    # schematic on disk (never saving the schematic for a view copy).
    assert turn_end_calls(rig) == [({"pcb"}, False), ({"sch"}, False)]
    assert rig.disk == 1


async def test_kicad_failed_after_live_frames_sends_a_turn_end_frame(rig: Rig) -> None:
    await live_frame_sent(rig)
    await rig.opened.hooks.on_turn_end(turn(), done("sch", reason="kicad_failed", failed=True))
    assert turn_end_calls(rig) == [({"sch"}, False)] and rig.disk == 1


async def test_inconsistent_after_live_frames_sends_a_frame_from_the_reopened_session(
    rig: Rig,
) -> None:
    env, project = rig.opened.env, rig.opened.project
    await live_frame_sent(rig)
    outcome = TurnOutcome(
        reason="completed",
        changed_docs=frozenset({"sch"}),
        mutated=True,
        document_inconsistent=True,
    )
    await rig.opened.hooks.on_turn_end(turn(), outcome)
    assert turn_end_calls(rig) == [({"sch"}, False)]
    fresh = env.svc.open_session(project)
    assert fresh is not None and fresh is not rig.opened.session
    frames = [f for f in env.broadcaster.frames if isinstance(f, ViewUpdatedFrame)]
    assert frames[-1].version == fresh.view_version and frames[-1].live is None


async def test_autosave_failure_after_live_frames_announces_the_disk_state(
    rig: Rig,
) -> None:
    env = rig.opened.env
    await live_frame_sent(rig)
    rig.opened.session.modified_docs.add("sch")  # the turn's last edit could not save itself
    env.save_error = RuntimeError("disk full")
    written = list(env.written)
    await rig.opened.hooks.on_turn_end(turn(), done("sch"))
    assert turn_end_calls(rig) == [({"sch"}, False)] and rig.disk == 1
    assert env.written == written  # no view copy: it would save the schematic
    assert env.saved == []


async def test_closed_session_after_live_frames_sends_nothing(rig: Rig) -> None:
    await live_frame_sent(rig)
    await rig.opened.env.sessions.close_all()
    await rig.opened.hooks.on_turn_end(turn(), done("sch"))
    assert turn_end_calls(rig) == [({"sch"}, False)]  # the catch-up finds no session
    frames = [f for f in rig.opened.env.broadcaster.frames if isinstance(f, ViewUpdatedFrame)]
    assert frames == []


async def test_failed_turn_without_live_frames_sends_nothing(rig: Rig) -> None:
    await rig.opened.hooks.on_turn_end(turn(), done("sch", reason="kicad_failed", failed=True))
    assert rig.calls == []


async def test_catch_up_failure_only_logs(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    await live_frame_sent(rig)
    rig.error = RuntimeError("SaveCopy failed")
    with caplog.at_level(logging.WARNING, logger="copperchat.history.hooks"):
        await rig.opened.hooks.on_turn_end(turn(), done("sch", reason="kicad_failed", failed=True))
    assert "after live frames failed" in caplog.text


async def test_turn_end_that_raises_still_catches_up(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await live_frame_sent(rig)
    hooks = rig.opened.hooks

    async def boom(*_: Any) -> bool:
        raise RuntimeError("recovery broke")

    monkeypatch.setattr(hooks, "_turn_end", boom)
    with pytest.raises(RuntimeError, match="recovery broke"):
        await hooks.on_turn_end(turn(), done("sch", reason="kicad_failed", failed=True))
    assert rig.disk == 1  # the catch-up still ran, then the error propagated


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


# ---- the catch-up copies the schematic on disk (the last save), never through KiCad


def disk_rig(opened: Opened) -> tuple[FakeTime, LiveViewPublisher, dict[str, bytes]]:
    """The real publish_view behind a fake writer that copies `content` into the view."""
    project, svc = opened.project, opened.env.svc
    content = {"sch": b"(kicad_sch v1)"}

    async def write_view(session: KicadSession, doc: DocKind, *, save_modified: bool = True) -> Any:
        path = snapshot_path(project, doc)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.get(doc, b"(board)"))
        return path

    svc.write_view = write_view  # type: ignore[assignment]
    t, live = real_rig(opened)
    return t, live, content


def kicad_calls_during_catch_up(
    opened: Opened, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[str], list[int]]:
    """KiCad calls / server starts made while `_catch_up_view` runs."""
    calls: list[str] = []
    starts: list[int] = []
    inside = False
    hooks, server = opened.hooks, opened.env.server
    real_catch_up = hooks._catch_up_view

    async def catch_up() -> None:
        nonlocal inside
        inside, before = True, server.starts
        try:
            await real_catch_up()
        finally:
            inside = False
            starts.append(server.starts - before)

    def record(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapper(self: KicadSession, *a: Any, **kw: Any) -> Any:
            if inside:
                calls.append(name)
            return await real(self, *a, **kw)

        return wrapper

    for name in ("call", "save", "save_copy", "start"):
        monkeypatch.setattr(KicadSession, name, record(name, getattr(KicadSession, name)))
    hooks._catch_up_view = catch_up  # type: ignore[method-assign]
    return calls, starts


async def test_catch_up_announces_the_last_save_from_disk(
    opened: Opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1 published live; E2 saved itself but its live publish was cancelled by the turn
    end; E3 could not save itself and the autosave failed. The catch-up frame lists the
    root and the sub-sheet with E2's content from disk."""
    env, project, session = opened.env, opened.project, opened.session
    t, live, content = disk_rig(opened)
    root, sub = snapshot_path(project, "sch").name, "sub.kicad_sch"
    view = snapshot_path(project, "sch").parent
    view.mkdir(parents=True, exist_ok=True)
    (project.dir / sub).write_bytes(b"(sub v1)")
    (view / sub).write_bytes(b"(sub v1)")
    await env.svc.publish_view(project, session, {"sch"})  # what the clients have
    content["sch"] = b"(kicad_sch E1)"
    opened.hooks.on_documents_changed(turn(), {"sch"})  # E1
    await t.advance(2.0)
    await until(lambda: len(view_frames(opened)) == 2)
    await until(lambda: not live._states[project.id].publishing)
    assert project.sch_file is not None
    project.sch_file.write_bytes(b"(kicad_sch E2)")  # E2 saved itself ...
    (project.dir / sub).write_bytes(b"(sub E2)")
    opened.hooks.on_documents_changed(turn(), {"sch"})  # ... its live publish is pending
    session.modified_docs.add("sch")  # E3 could not save itself
    env.save_error = RuntimeError("disk full")  # nor can the autosave
    calls, starts = kicad_calls_during_catch_up(opened, monkeypatch)
    await opened.hooks.on_turn_end(turn(), done("sch"))
    assert view_frames(opened)[2:] == [(3, sorted([root, sub]), None)]
    assert (view / root).read_bytes() == b"(kicad_sch E2)"
    assert (view / sub).read_bytes() == b"(sub E2)"
    assert calls == [] and starts == [0]
    assert env.saved == [] and env.server.saved == []


async def test_kicad_down_after_kicad_failed_catch_up_never_calls_kicad(
    opened: Opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, project = opened.env, opened.project
    t, live, content = disk_rig(opened)
    await env.svc.publish_view(project, opened.session, {"sch"})
    content["sch"] = b"(kicad_sch E1)"
    opened.hooks.on_documents_changed(turn(), {"sch"})
    await t.advance(2.0)
    await until(lambda: len(view_frames(opened)) == 2)
    await until(lambda: not live._states[project.id].publishing)
    env.server.crash()
    env.server.fail_start_with_exit_code = 1  # every restart fails
    calls, starts = kicad_calls_during_catch_up(opened, monkeypatch)
    await opened.hooks.on_turn_end(turn(), done("sch", reason="kicad_failed", failed=True))
    assert any(meta == {"code": "kicad_restart_failed"} for _, meta in env.engine.errors)
    assert calls == [] and starts == [0]
    assert env.saved == [] and env.server.saved == []
    assert project.sch_file is not None
    assert view_frames(opened)[2:] == [(3, [snapshot_path(project, "sch").name], None)]
    assert snapshot_path(project, "sch").read_bytes() == project.sch_file.read_bytes()


async def test_autosave_publish_failure_after_live_frames_catches_up_once(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    svc = rig.opened.env.svc
    await live_frame_sent(rig)
    recording, recording_disk = svc.publish_view, svc.publish_disk_view
    failed: list[bool] = []
    locked: list[bool] = []

    async def failing_once(p: Project, session: KicadSession, docs: Any, **kw: Any) -> int:
        if not kw.get("live") and not failed:
            failed.append(True)
            rig.calls.append((rig.time.now, set(docs), False))
            raise RuntimeError("SaveCopy failed")
        return await recording(p, session, docs, **kw)

    async def disk(p: Project) -> int | None:
        locked.append(svc.lock(p).locked())
        return await recording_disk(p)

    svc.publish_view = failing_once  # type: ignore[method-assign]
    svc.publish_disk_view = disk  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="copperchat.history.hooks"):
        await rig.opened.hooks.on_turn_end(turn(), done("sch"))
    assert rig.opened.env.saved == []  # every edit saved itself: nothing to autosave
    assert turn_end_calls(rig) == [({"sch"}, False), ({"sch"}, False)]
    assert rig.disk == 1 and locked == [True]
    assert "view refresh after autosave failed" in caplog.text
    frames = [f for f in rig.opened.env.broadcaster.frames if isinstance(f, ViewUpdatedFrame)]
    assert [f.live for f in frames] == [None]
    assert frames[0].version == rig.opened.session.view_version


async def test_read_only_session_after_the_inconsistent_reopen_catches_up(
    rig: Rig,
) -> None:
    env, project = rig.opened.env, rig.opened.project
    await live_frame_sent(rig)
    real_reopen = env.sessions.reopen

    async def reopen_read_only(p: Project) -> KicadSession:
        fresh = await real_reopen(p)
        fresh.read_only = True  # e.g. KiCad's GUI took the lock meanwhile
        return fresh

    env.sessions.reopen = reopen_read_only  # type: ignore[method-assign]
    outcome = TurnOutcome(
        reason="completed",
        changed_docs=frozenset({"sch"}),
        mutated=True,
        document_inconsistent=True,
    )
    await rig.opened.hooks.on_turn_end(turn(), outcome)
    fresh = env.svc.open_session(project)
    assert fresh is not None and fresh.read_only
    assert turn_end_calls(rig) == [({"sch"}, False)] and rig.disk == 1
    frames = [f for f in env.broadcaster.frames if isinstance(f, ViewUpdatedFrame)]
    assert [(f.version, f.live) for f in frames] == [(fresh.view_version, None)]
    assert env.saved == [] and env.server.saved == []
