"""Which WebSocket connections currently show CopperChat in the foreground."""

from __future__ import annotations


class VisibilityRegistry:
    """Connections that reported ``visibility{visible: true}``. One user per instance, so
    "no client of that user is visible" is simply "no visible connection" (any visible
    connection suppresses every push)."""

    def __init__(self) -> None:
        self._visible: dict[int, str] = {}

    def set(self, conn_id: int, project_id: str, visible: bool) -> None:
        if visible:
            self._visible[conn_id] = project_id
        else:
            self._visible.pop(conn_id, None)

    def drop(self, conn_id: int) -> None:
        self._visible.pop(conn_id, None)

    def any_visible(self) -> bool:
        return bool(self._visible)
