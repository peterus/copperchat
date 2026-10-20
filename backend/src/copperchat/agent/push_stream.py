# backend/src/copperchat/agent/push_stream.py
"""Push-based async input stream (port of Kontor inputStream.ts) with a turn gate."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from typing import Protocol


class _HasMessageId(Protocol):
    @property
    def message_id(self) -> int: ...


class PushStream[T: _HasMessageId]:
    """Bound to one conversation (spec §5.2): the engine pushes only that conversation's
    items; a foreign item seals the stream instead."""

    def __init__(self, conversation_id: str = "") -> None:
        self.conversation_id = conversation_id
        self._queue: deque[T] = deque()
        self._closing = False
        self._sealed = False
        self.closed = False
        self._released = True  # the first item of a burst needs no release
        self._wake = asyncio.Event()

    def push(self, item: T) -> bool:
        """Enqueue; False if the generator already returned (caller starts a new burst) or
        the stream is sealed."""
        if self.closed or self._sealed:
            return False
        self._queue.append(item)
        self._closing = False  # a fresh message cancels a pending close (reopen)
        self._wake.set()
        return True

    def request_close(self) -> None:
        """End the stream once the queue is empty and no push re-opened it."""
        self._closing = True
        self._wake.set()

    def seal(self) -> None:
        """Like request_close, but no push can reopen it: the stream ends once the items
        already queued are drained."""
        self._sealed = True
        self.request_close()

    @property
    def sealed(self) -> bool:
        return self._sealed

    def release_next(self) -> None:
        """Called by the engine after it processed the previous turn's TurnFinished."""
        self._released = True
        self._wake.set()

    def drop(self, message_id: int) -> bool:
        for queued in self._queue:
            if queued.message_id == message_id:
                self._queue.remove(queued)
                self._wake.set()
                return True
        return False

    def pending_count(self) -> int:
        return len(self._queue)

    def pending_ids(self) -> list[int]:
        return [q.message_id for q in self._queue]

    def __aiter__(self) -> AsyncIterator[T]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[T]:
        while True:
            # Queue-first ordering is load-bearing: drain before honouring close.
            if self._queue and self._released:
                self._released = False
                yield self._queue.popleft()
                continue
            if not self._queue and self._closing:
                self.closed = True
                return
            self._wake.clear()
            await self._wake.wait()
