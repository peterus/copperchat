"""Which WebSocket connections currently show CopperChat in the foreground."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

# A "visible" claim only holds while its connection keeps sending frames. The client pings
# every 25 s (frontend/src/api/ws.ts), so 60 s tolerates one lost ping. A page that iOS
# froze in the background never sends visible:false and its socket can stay open for hours
# (the server's own heartbeat keeps the proxies from idling it out): after the TTL it no
# longer suppresses pushes.
VISIBLE_TTL_S = 60.0


@dataclass
class _Entry:
    project_id: str
    visible: bool
    seen: float


class VisibilityRegistry:
    """Connections that reported ``visibility{visible, projectId}``, with the time of their
    last frame. One user per instance: a push about a project is suppressed while a live
    connection shows that project in the foreground."""

    def __init__(
        self, ttl_s: float = VISIBLE_TTL_S, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.ttl_s = ttl_s
        self._clock = clock
        self._entries: dict[int, _Entry] = {}

    def set(self, conn_id: int, project_id: str, visible: bool) -> None:
        self._entries[conn_id] = _Entry(project_id, visible, self._clock())

    def touch(self, conn_id: int) -> None:
        """Any frame from the connection proves its page is still running."""
        entry = self._entries.get(conn_id)
        if entry is not None:
            entry.seen = self._clock()

    def drop(self, conn_id: int) -> None:
        self._entries.pop(conn_id, None)

    def visible_for(self, project_id: str) -> bool:
        cutoff = self._clock() - self.ttl_s
        return any(
            e.visible and e.project_id == project_id and e.seen >= cutoff
            for e in self._entries.values()
        )
