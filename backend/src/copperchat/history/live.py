"""Near-live schematic view: debounced `publish_view(..., live=True)` while a turn edits.

Every schematic change of a turn (engine `on_documents_changed`) schedules a publish:
trailing debounce, at most one live publish per min interval, and at most max wait after
the first unpublished change (continuous edits still refresh the viewer). The publish
runs under `HistoryService.lock`, so it never overlaps a restore, the turn-start check or
the turn-end autosave. The turn end (`finish`) stops it before the autosave; its own
publish is the guaranteed final state: after live frames, every turn end (also a failed
one) sends a non-live schematic frame if a session is open. A failed live publish only
lets the viewer lag. The board stays turn-end only. A schematic with unsaved edits (a
failed partial edit, which the turn end reloads from disk) is never saved for a live
publish: that publish is skipped.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from copperchat.kicad_session import DocumentModified
from copperchat.projects import Project

if TYPE_CHECKING:
    from copperchat.history.service import HistoryService

log = logging.getLogger(__name__)

LIVE_VIEW_DEBOUNCE_S = 2.0  # trailing: publish once the edits pause this long
LIVE_VIEW_MIN_INTERVAL_S = 2.5  # between the end of one live publish and the next
LIVE_VIEW_MAX_WAIT_S = 6.0  # after the first unpublished change, even if edits go on


@dataclass
class _State:
    first: float | None = None  # first unpublished change (None: nothing pending)
    last: float = 0.0  # last change
    last_publish: float = -math.inf
    publishing: bool = False
    sent: bool = False  # a live frame went out this turn (the turn end must catch up)
    stopped: bool = False  # finish/close: the task must not publish (again)
    task: asyncio.Task[None] | None = None


class LiveViewPublisher:
    """One per HistoryService; per-project state lives from the first schematic change
    of a turn until `finish` (turn end) or `close` (engine close)."""

    def __init__(
        self,
        svc: HistoryService,
        *,
        debounce_s: float = LIVE_VIEW_DEBOUNCE_S,
        min_interval_s: float = LIVE_VIEW_MIN_INTERVAL_S,
        max_wait_s: float = LIVE_VIEW_MAX_WAIT_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.svc = svc
        self.debounce_s = debounce_s
        self.min_interval_s = min_interval_s
        self.max_wait_s = max_wait_s
        self._clock = clock
        self._sleep = sleep
        self._states: dict[str, _State] = {}

    def schedule(self, project: Project) -> None:
        """A tool call changed the schematic (sync: never waits for the publish)."""
        now = self._clock()
        st = self._states.setdefault(project.id, _State())
        st.last = now
        if st.first is None:
            st.first = now
        if st.task is None or st.task.done():
            st.task = asyncio.get_running_loop().create_task(
                self._run(project, st), name=f"live-view-{project.id}"
            )

    def _deadline(self, st: _State) -> float:
        assert st.first is not None
        settled = min(st.last + self.debounce_s, st.first + self.max_wait_s)
        return max(settled, st.last_publish + self.min_interval_s)

    async def _run(self, project: Project, st: _State) -> None:
        while st.first is not None and not st.stopped:
            # Re-evaluated after every sleep: a change meanwhile moves the deadline.
            while (delay := self._deadline(st) - self._clock()) > 0:
                await self._sleep(delay)
            st.first = None
            st.publishing = True
            try:
                if await self._publish(project):
                    st.sent = True
            finally:
                st.publishing = False
                st.last_publish = self._clock()

    async def _publish(self, project: Project) -> bool:
        """True if a live frame was sent (a publish that changed nothing sends none)."""
        svc = self.svc
        try:
            session = svc.open_session(project)
            if session is None or session.read_only or "sch" not in svc.docs_of(project):
                return False
            async with svc.lock(project):
                if svc.open_session(project) is not session:
                    return False  # restored / reloaded meanwhile: that path published itself
                before = session.view_version
                # Only a sent frame bumps the version; the lock keeps others out.
                return await svc.publish_view(project, session, {"sch"}, live=True) != before
        except DocumentModified:  # a failed partial edit: never saved, the turn end reloads
            log.info("live view publish for %s skipped: unsaved edits", project.rel_path)
        except Exception:  # only the viewer lags; the turn and its turn-end publish go on
            log.warning("live view publish for %s failed", project.rel_path, exc_info=True)
        return False

    async def finish(self, project: Project) -> bool:
        """Turn end (every outcome): afterwards nothing of the turn publishes any more.
        A sleeping task is cancelled; a running publish is awaited, never cancelled (a
        KiCad call cancelled mid-way can restart the api-server). If the caller itself is
        cancelled meanwhile, the task is cancelled too: its state is gone, so no later
        `close` could reach it. Returns whether a live frame was sent this turn: then the
        turn end owes the clients a non-live schematic frame (Live-off clients and the
        agent view only follow those)."""
        st = self._states.pop(project.id, None)
        if st is None or st.task is None:
            return False
        st.stopped = True
        if not st.publishing:
            st.task.cancel()
        try:
            await _wait(st.task)
        except asyncio.CancelledError:
            st.task.cancel()
            raise
        return st.sent

    async def close(self, project: Project) -> None:
        """Engine close (shutdown): cancel unconditionally."""
        st = self._states.pop(project.id, None)
        if st is None or st.task is None:
            return
        st.stopped = True
        st.task.cancel()
        await _wait(st.task)


async def _wait(task: asyncio.Task[None]) -> None:
    """Until `task` is done; its cancellation is not ours (ours still propagates)."""
    await asyncio.wait({task})
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.warning("live view task failed", exc_info=exc)
