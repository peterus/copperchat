"""Near-live schematic view: debounced `publish_view(..., live=True)` while a turn edits.

Every schematic change of a turn (engine `on_documents_changed`) schedules a publish:
trailing debounce, at most one live publish per min interval, and at most max wait after
the first unpublished change (continuous edits still refresh the viewer). The publish
runs under `HistoryService.lock`, so it never overlaps a restore, the turn-start check or
the turn-end autosave. The turn end (`finish`) stops it before the autosave; its own
publish is the guaranteed final state. A failed live publish only lets the viewer lag.
The board stays turn-end only.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

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
                await self._publish(project)
            finally:
                st.publishing = False
                st.last_publish = self._clock()

    async def _publish(self, project: Project) -> None:
        svc = self.svc
        try:
            session = svc.open_session(project)
            if session is None or session.read_only or "sch" not in svc.docs_of(project):
                return
            async with svc.lock(project):
                if svc.open_session(project) is not session:
                    return  # restored / reloaded meanwhile: that path published itself
                await svc.publish_view(project, session, {"sch"}, live=True)
        except Exception:  # only the viewer lags; the turn and its turn-end publish go on
            log.warning("live view publish for %s failed", project.rel_path, exc_info=True)

    async def finish(self, project: Project) -> None:
        """Turn end (every outcome): afterwards nothing of the turn publishes any more.
        A sleeping task is cancelled; a running publish is awaited, never cancelled (a
        KiCad call cancelled mid-way can restart the api-server)."""
        st = self._states.pop(project.id, None)
        if st is None or st.task is None:
            return
        st.stopped = True
        if not st.publishing:
            st.task.cancel()
        await _wait(st.task)

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
