"""Turns broadcast frames into Web Push notifications (spec §9).

| frame                                   | notification                    |
|-----------------------------------------|---------------------------------|
| run-finished completed                  | "run finished" + final text     |
| run-finished error / final text ends "?"| "needs your attention"          |
| rate-limited{resetsAt}                  | at resetsAt: "rate limit window reopened" |
| run-finished interrupted / rate_limited | none                            |

Nothing is sent while any client reports itself visible. ``on_frame`` is the
Broadcaster listener: it only enqueues, so publishing (which runs under the engine's
lock) never waits for the database or the push service.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from copperchat.agent.protocol import RateLimitedFrame, RunFinishedFrame, Wire
from copperchat.db import Database
from copperchat.push.visibility import VisibilityRegistry

log = logging.getLogger(__name__)
BODY_MAX = 120


class Dispatcher(Protocol):
    async def send_all(self, payload: dict[str, Any]) -> int: ...


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= BODY_MAX else text[: BODY_MAX - 1].rstrip() + "…"


def _parse_ts(value: str) -> datetime | None:
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


class PushNotifier:
    def __init__(
        self,
        db: Database,
        dispatcher: Dispatcher,
        visibility: VisibilityRegistry,
        project_names: Callable[[str], str],
    ) -> None:
        self.db = db
        self.dispatcher = dispatcher
        self.visibility = visibility
        self.project_names = project_names
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._queue: asyncio.Queue[Wire] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._closed = False

    # ---- Broadcaster listener (sync, never blocks the publisher)
    def on_frame(self, frame: Wire) -> None:
        if self._closed or not isinstance(frame, RunFinishedFrame | RateLimitedFrame):
            return
        self._queue.put_nowait(frame)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._work())

    async def _work(self) -> None:
        while not self._queue.empty():
            frame = self._queue.get_nowait()
            try:
                await self.handle(frame)
            except Exception:
                log.exception("push notification for %s failed", frame)

    async def drain(self) -> None:
        """Wait until every frame seen so far is handled (tests, shutdown)."""
        while self._worker is not None and not self._worker.done():
            await asyncio.shield(self._worker)

    # ---- frame handling
    async def handle(self, frame: Wire) -> None:
        if isinstance(frame, RunFinishedFrame):
            await self._run_finished(frame)
        elif isinstance(frame, RateLimitedFrame):
            when = _parse_ts(frame.resets_at)
            if when is not None:
                self.schedule_reopen(frame.project_id, when)

    async def _final_row(self, project_id: str, run_id: str) -> tuple[str, str] | None:
        """The run's last top-level assistant text or error row (sub-agent rows excluded)."""
        row = await self.db.fetchone(
            "SELECT kind, content FROM messages WHERE project_id = ? AND run_id = ? "
            "AND ((role = 'assistant' AND kind = 'text') OR kind = 'error') "
            "AND json_extract(metadata, '$.parentToolUseId') IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (project_id, run_id),
        )
        return None if row is None else (str(row["kind"]), str(row["content"]).strip())

    async def _run_finished(self, f: RunFinishedFrame) -> None:
        if f.reason not in ("completed", "error"):
            return  # Stop pressed by the user, or parked (the reopen push covers that)
        if self.visibility.any_visible():
            return
        final = await self._final_row(f.project_id, f.run_id)
        kind, text = final if final is not None else ("", "")
        attention = f.reason == "error" or kind == "error" or text.endswith("?")
        title = "needs your attention" if attention else "run finished"
        if text:
            body = _clip(text)
        elif f.reason == "error":
            body = "The run ended with an error."
        else:
            body = "Done."
        await self._send(f.project_id, title, body, tag=f"run-{f.project_id}")

    def schedule_reopen(self, project_id: str, resets_at: datetime) -> None:
        if self._closed:
            return
        if (old := self._timers.pop(project_id, None)) is not None:
            old.cancel()
        delay = max(0.0, (resets_at - datetime.now(UTC)).total_seconds())

        async def fire() -> None:
            await asyncio.sleep(delay)
            if self._timers.get(project_id) is task:
                del self._timers[project_id]
            try:
                await self._send(
                    project_id,
                    "rate limit window reopened",
                    "Queued messages continue now.",
                    tag=f"ratelimit-{project_id}",
                )
            except Exception:
                log.exception("rate-limit push for %s failed", project_id)

        task = asyncio.get_running_loop().create_task(fire())
        self._timers[project_id] = task

    async def _send(self, project_id: str, title: str, body: str, *, tag: str) -> None:
        if self.visibility.any_visible():
            return
        await self.dispatcher.send_all(
            {
                "title": f"{self.project_names(project_id)}: {title}",
                "body": body,
                "url": f"/p/{project_id}",
                "tag": tag,
            }
        )

    async def close(self) -> None:
        self._closed = True
        tasks = [*self._timers.values()]
        if self._worker is not None:
            tasks.append(self._worker)
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._timers.clear()
