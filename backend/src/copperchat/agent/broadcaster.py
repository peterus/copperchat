# backend/src/copperchat/agent/broadcaster.py
"""Per-project fan-out of server frames with bounded per-subscriber queues."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from copperchat.agent.protocol import Wire

log = logging.getLogger(__name__)

# Called synchronously for every published frame (all projects); must not block. Errors
# are logged and never reach the publisher.
Listener = Callable[[Wire], None]


class Subscription:
    """Async iterator of frames. A subscriber that falls `max_queue` frames behind is
    dropped: its iterator ends and `dropped` is True (the client reconnects and replays)."""

    def __init__(self, max_queue: int, on_close: Callable[[Subscription], None]) -> None:
        self._q: asyncio.Queue[Wire | None] = asyncio.Queue(maxsize=max_queue + 1)
        self._max = max_queue
        self._on_close = on_close
        self._closed = False
        self.dropped = False

    def _offer(self, frame: Wire) -> None:
        if self._closed:
            return
        if self._q.qsize() >= self._max:
            self.dropped = True
            log.warning("dropping slow websocket subscriber")
            self.close()
            return
        self._q.put_nowait(frame)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        while not self._q.empty():
            self._q.get_nowait()
        self._q.put_nowait(None)  # end-of-stream sentinel (queue has room for it)
        self._on_close(self)

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Wire:
        frame = await self._q.get()
        if frame is None:
            raise StopAsyncIteration
        return frame


class Broadcaster:
    def __init__(self, max_queue: int = 1000) -> None:
        self._max = max_queue
        self._subs: dict[str, set[Subscription]] = {}
        self._listeners: list[Listener] = []

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def subscribe(self, project_id: str) -> Subscription:
        sub = Subscription(self._max, lambda s: self._subs.get(project_id, set()).discard(s))
        self._subs.setdefault(project_id, set()).add(sub)
        return sub

    def subscriber_count(self, project_id: str) -> int:
        return len(self._subs.get(project_id, ()))

    async def publish(self, project_id: str, frame: Wire) -> None:
        for sub in list(self._subs.get(project_id, ())):
            sub._offer(frame)
        for listener in self._listeners:
            try:
                listener(frame)
            except Exception:
                log.exception("broadcast listener failed")
