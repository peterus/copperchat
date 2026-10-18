"""Per-project run engine: queue-first persistence, gated input, streaming, hooks."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from copperchat.agent.broadcaster import Broadcaster, Subscription
from copperchat.agent.events import (
    AgentBackend,
    AgentEvent,
    QueuedItem,
    RateLimited,
    SessionId,
    SubagentText,
    TextDelta,
    ThinkingDelta,
    ToolResultEvt,
    ToolUse,
    TurnFinished,
    Usage,
)
from copperchat.agent.protocol import (
    AttachmentRef,
    FinishReason,
    MessageDeltaFrame,
    MessageDTO,
    MessageFrame,
    RateLimitedFrame,
    RateLimitPayload,
    RunFinishedFrame,
    RunStartedFrame,
    UsageFrame,
    UsagePayload,
    Wire,
)
from copperchat.agent.push_stream import PushStream
from copperchat.agent.sdk_tools import SERVER_NAME
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.db.repos.agent_state import AgentSessionRepo, RateLimitRepo, RunRepo
from copperchat.db.repos.messages import MessageRepo, MessageRow
from copperchat.errors import CopperChatError
from copperchat.kicad_session import DocKind, KicadSessionManager, KicadUnavailable
from copperchat.projects import Project, agent_dir
from copperchat.tools import ImageBlock, ToolContext

if TYPE_CHECKING:
    from copperchat.editing.service import EditingService

log = logging.getLogger(__name__)
_IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
# Sub-agent rows are clipped (C7a): a Write of a whole notes.md or a fetched web page must
# not bloat the chat.
_SUB_INPUT_MAX = 2000
_SUB_RESULT_MAX = 4000


def _clip(value: Any) -> Any:
    """Truncate every string inside a (JSON-like) tool input to _SUB_INPUT_MAX chars."""
    if isinstance(value, str):
        return value[:_SUB_INPUT_MAX]
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v) for v in value]
    return value


AttachmentResolver = Callable[[AttachmentRef], Awaitable[AttachmentRef | None]]


async def _no_attachments(_ref: AttachmentRef) -> AttachmentRef | None:
    return None


class UnknownAttachment(CopperChatError):
    pass


@dataclass(frozen=True)
class TurnInfo:
    run_id: str
    message_id: int
    text: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class TurnOutcome:
    reason: FinishReason
    changed_docs: frozenset[DocKind] = frozenset()
    mutated: bool = False
    kicad_failed: bool = False
    # A failed edit could not be undone exactly (E-C4): like kicad_failed, no autosave;
    # the session is reopened from disk. auto_restore: restore the last step checkpoint /
    # turn snapshot (owner decision 4).
    document_inconsistent: bool = False
    auto_restore: bool = False


class RunHooks:
    """No-op extension points; Phase 7 subclasses this."""

    async def on_engine_start(self) -> None:
        return None

    async def on_turn_start(self, turn: TurnInfo) -> None:
        return None

    async def on_first_mutation(self, turn: TurnInfo) -> None:
        return None

    async def on_turn_end(self, turn: TurnInfo, outcome: TurnOutcome) -> None:
        return None


class ChainedHooks(RunHooks):
    """Several hooks in order (history first, E-C5). A failing start/mutation hook stops
    the chain and propagates (a failed snapshot must fail the tool call); turn-end hooks
    all run, then the first error propagates."""

    def __init__(self, hooks: list[RunHooks]) -> None:
        self.hooks = hooks

    async def on_engine_start(self) -> None:
        for h in self.hooks:
            await h.on_engine_start()

    async def on_turn_start(self, turn: TurnInfo) -> None:
        for h in self.hooks:
            await h.on_turn_start(turn)

    async def on_first_mutation(self, turn: TurnInfo) -> None:
        for h in self.hooks:
            await h.on_first_mutation(turn)

    async def on_turn_end(self, turn: TurnInfo, outcome: TurnOutcome) -> None:
        first: Exception | None = None
        for h in self.hooks:
            try:
                await h.on_turn_end(turn, outcome)
            except Exception as exc:
                first = first or exc
        if first is not None:
            raise first


BackendFactory = Callable[[Callable[[], ToolContext]], AgentBackend]


@dataclass
class _Segment:
    id: int
    kind: str
    content: str = ""
    flushed_len: int = 0
    last_flush: float = 0.0


@dataclass
class _Turn:
    info: TurnInfo
    seg: _Segment | None = None
    usage: dict[str, Any] | None = None
    rate_limited: datetime | None = None
    changed: set[DocKind] = field(default_factory=set)
    mutated: bool = False
    kicad_failed: bool = False
    kicad_reported: bool = False
    document_inconsistent: bool = False
    auto_restore: bool = False


class RunEngine:
    def __init__(
        self,
        *,
        project: Project,
        settings: Settings,
        db: Database,
        broadcaster: Broadcaster,
        sessions: KicadSessionManager,
        backend_factory: BackendFactory,
        hooks: RunHooks | None = None,
        flush_interval_s: float = 0.5,
        flush_chars: int = 2048,
        interrupt_grace_s: float = 5.0,
    ) -> None:
        self.project = project
        self.broadcaster = broadcaster
        self.hooks = hooks or RunHooks()
        # SP2 (E-C5): set by AgentRuntime.ensure; passed into every ToolContext.
        self.editing: EditingService | None = None
        # Phase 8 wires the uploads table (AgentRuntime); unknown ids are rejected.
        self.attachment_resolver: AttachmentResolver = _no_attachments
        self.rate_limited_until: datetime | None = None
        self._settings = settings
        self._sessions = sessions
        self._messages = MessageRepo(db)
        self._runs = RunRepo(db)
        self._agent_sessions = AgentSessionRepo(db)
        self._rate_limits = RateLimitRepo(db)
        self._flush_interval = flush_interval_s
        self._flush_chars = flush_chars
        self._grace = interrupt_grace_s
        self._lock = asyncio.Lock()  # serialises persist+publish against replay snapshots
        self._stream: PushStream[QueuedItem] | None = None
        self._burst: asyncio.Task[None] | None = None  # newest burst
        self._bursts: set[asyncio.Task[None]] = set()  # incl. ones still winding down
        self._turn: _Turn | None = None
        self._inflight: int | None = None  # pulled from the stream, turn not finished
        self._live: dict[int, str] = {}  # streaming row id -> full content incl. unflushed
        self._notes: list[str] = []
        self._interrupting = False
        self._closed = False
        self._resume_timer: asyncio.TimerHandle | None = None
        self._bg: set[asyncio.Task[Any]] = set()
        self.backend: AgentBackend = backend_factory(self._make_ctx)

    # ---- public surface
    @property
    def pid(self) -> str:
        return self.project.id

    @property
    def is_busy(self) -> bool:
        return self._burst is not None and not self._burst.done()

    @property
    def current_run_id(self) -> str | None:
        return self._turn.info.run_id if self._turn else None

    async def start(self) -> None:
        await self._messages.finalize_streaming(self.pid)
        await self._runs.finalize_open(self.pid)
        try:
            await self.hooks.on_engine_start()
        except Exception:
            log.exception("on_engine_start hook failed")
        await self._restore()

    async def close(self) -> None:
        self._closed = True
        if self._resume_timer is not None:
            self._resume_timer.cancel()
        bursts = [t for t in self._bursts if not t.done()]
        for task in bursts:
            task.cancel()
        for task in bursts:
            with suppress(asyncio.CancelledError, Exception):
                await task
        for t in list(self._bg):
            t.cancel()

    async def submit(
        self, client_message_id: str, text: str, attachments: list[AttachmentRef]
    ) -> MessageDTO:
        dto, _ = await self.submit_ex(client_message_id, text, attachments)
        return dto

    async def submit_ex(
        self,
        client_message_id: str,
        text: str,
        attachments: list[AttachmentRef],
        metadata: dict[str, Any] | None = None,
    ) -> tuple[MessageDTO, bool]:
        resolved: list[AttachmentRef] = []
        for att in attachments:
            found = await self.attachment_resolver(att)
            if found is None:
                raise UnknownAttachment(f"unknown attachment: {att.id}")
            resolved.append(found)
        attachments = resolved
        meta = dict(metadata or {})
        if attachments:
            meta["attachments"] = [a.model_dump(mode="json", by_alias=True) for a in attachments]
        async with self._lock:
            row, created = await self._messages.insert_user(self.pid, client_message_id, text, meta)
            dto = self.to_dto(row)
            await self._publish(MessageFrame(project_id=self.pid, message=dto))
            # Enqueue under the lock: _drive_queued scans under it too, so a 'queued' row
            # is always either in the stream, in flight, or not yet visible to the scan.
            if created and self.rate_limited_until is None:
                self._enqueue(self._item(row))
        return dto, created

    async def post_notice(self, text: str, metadata: dict[str, Any] | None = None) -> MessageDTO:
        async with self._lock:
            row = await self._insert(role="system", kind="notice", content=text, metadata=metadata)
        return self.to_dto(row)

    async def post_error(self, text: str, metadata: dict[str, Any] | None = None) -> MessageDTO:
        async with self._lock:
            row = await self._insert(role="system", kind="error", content=text, metadata=metadata)
        return self.to_dto(row)

    async def cancel_queued(self, message_id: int) -> bool:
        """True if the message was still waiting; False once the engine pulled it."""
        async with self._lock:
            row = await self._messages.get(message_id)
            if (
                row is None
                or row.project_id != self.pid
                or row.role != "user"
                or row.status != "queued"
                or message_id == self._inflight
            ):
                return False
            # A pulled item is in flight from the moment the gate yields it (no await in
            # between), so a queued row that is not in flight is either still in the
            # stream or waiting outside it (parked by a rate limit): both are cancellable.
            if self._stream is not None:
                self._stream.drop(message_id)
            await self._cancel_row(message_id)
            return True

    async def interrupt(self, run_id: str | None) -> None:
        """Stop: cancel every waiting message and end the running turn (C7).

        A Stop for an older run (``run_id`` no longer current) is ignored. If the backend
        does not end the interrupted turn within the grace period, the burst is cancelled
        (backstop); a message sent after the Stop is never hit by that backstop.
        """
        current = self.current_run_id
        if run_id is not None and current is not None and run_id != current:
            return  # stale Stop for an older run
        async with self._lock:
            stream = self._stream
            if stream is not None:
                ids = stream.pending_ids()
                for mid in ids:
                    stream.drop(mid)
            else:
                ids = [
                    r.id
                    for r in await self._messages.queued_user_rows(self.pid)
                    if r.id != self._inflight
                ]
            for mid in ids:
                await self._cancel_row(mid)
            target = self._inflight
            if target is not None:
                self._interrupting = True
        task = self._burst
        if task is None or task.done():
            return
        if stream is not None:
            stream.request_close()
        if target is None:
            return  # between turns: closing the input ends the burst
        try:
            await self.backend.interrupt()
        except Exception:
            log.exception("backend.interrupt() failed for %s", self.pid)
        self._spawn(self._backstop(task, target))

    async def _backstop(self, task: asyncio.Task[None], message_id: int) -> None:
        await asyncio.wait({task}, timeout=self._grace)
        if not task.done() and self._inflight == message_id:
            log.warning(
                "agent ignored interrupt for %.1fs; cancelling burst of %s", self._grace, self.pid
            )
            task.cancel()

    def add_agent_note(self, text: str) -> None:
        self._notes.append(text)

    async def replay_and_subscribe(
        self, since_message_id: int
    ) -> tuple[list[MessageDTO], Subscription, str | None]:
        """Snapshot + subscribe atomically w.r.t. publishes: no gap, no duplicate delta."""
        async with self._lock:
            rows = await self._messages.since(self.pid, since_message_id)
            dtos = [self.to_dto(r) for r in rows]
            sub = self.broadcaster.subscribe(self.pid)
            return dtos, sub, self.current_run_id

    def to_dto(self, row: MessageRow) -> MessageDTO:
        wire = row.to_wire()
        wire["content"] = self._live.get(row.id, row.content)
        return MessageDTO.model_validate(wire)

    # ---- queue and bursts
    def _item(self, row: MessageRow) -> QueuedItem:
        atts = tuple(AttachmentRef.model_validate(a) for a in row.metadata.get("attachments", []))
        return QueuedItem(
            message_id=row.id,
            client_message_id=row.client_message_id or "",
            text=row.content,
            attachments=atts,
        )

    def _enqueue(self, item: QueuedItem) -> None:
        if self._stream is not None and self._stream.push(item):
            return
        stream: PushStream[QueuedItem] = PushStream()
        stream.push(item)
        self._stream = stream
        # The previous burst may still be winding down (its input ended, the backend has
        # not returned yet); the new burst waits for it so two backend clients never overlap
        # and the new one resumes from the session id the old one stored.
        prev = self._burst if self.is_busy else None
        task = asyncio.create_task(self._run_burst(stream, prev), name=f"burst-{self.pid}")
        self._burst = task
        self._bursts.add(task)
        task.add_done_callback(self._bursts.discard)

    async def _restore(self) -> None:
        resets = await self._rate_limits.get(self.pid)
        if resets is not None and resets.tzinfo is None:
            resets = resets.replace(tzinfo=UTC)  # stored by an older writer without offset
        if resets is not None and resets > datetime.now(UTC):
            self.rate_limited_until = resets  # still parked across the restart
            self._arm_resume(resets)
            return
        if resets is not None:
            await self._rate_limits.set(self.pid, None)
        await self._drive_queued()

    async def _drive_queued(self) -> None:
        if self._closed or self.rate_limited_until is not None:
            return
        async with self._lock:  # see submit_ex: scan and enqueue atomically
            rows = await self._messages.queued_user_rows(self.pid)
            known = set(self._stream.pending_ids()) if self._stream else set()
            for row in rows:
                if row.id not in known and row.id != self._inflight:
                    self._enqueue(self._item(row))

    async def _run_burst(
        self, stream: PushStream[QueuedItem], prev: asyncio.Task[None] | None = None
    ) -> None:
        flusher: asyncio.Task[None] | None = None
        try:
            if prev is not None:
                await self._await_wind_down(prev)
            # Read once per burst, never mid-burst.
            resume = await self._agent_sessions.get(self.pid)
            flusher = asyncio.create_task(self._flush_loop())
            async for ev in self.backend.start(self.project, self._gated(stream), resume):
                await self._on_event(ev, stream)
        except asyncio.CancelledError:
            if not self._interrupting:
                raise
            log.warning("burst for %s cancelled by the interrupt backstop", self.pid)
        except Exception as exc:
            log.exception("agent backend failed for %s", self.pid)
            turn = self._turn
            if turn is not None and (resets_at := self._park_until(turn)) is not None:
                # The backend failed after reporting a rejected rate limit: park, not error.
                await self._park(turn, stream, resets_at)
            elif turn is not None:
                async with self._lock:
                    await self._insert(
                        role="system",
                        kind="error",
                        content=f"The agent stopped unexpectedly: {exc}",
                    )
                await self._finish_turn("error")
            else:
                await self._fail_burst(stream, exc)
        finally:
            if flusher is not None:
                flusher.cancel()
            turn = self._turn
            resets_at = self._park_until(turn) if turn is not None else None
            if turn is not None and resets_at is not None and not self._closed:
                await self._park(turn, stream, resets_at)
            elif turn is not None:
                await self._finish_turn(
                    "interrupted" if (self._interrupting or self._closed) else "error"
                )
            if self._stream is stream:
                self._stream = None
            if self._burst is asyncio.current_task():
                self._burst = None
            await self._drive_queued()

    async def _fail_burst(self, stream: PushStream[QueuedItem], exc: Exception) -> None:
        """The backend failed with no turn in flight (typically before the first one, e.g.
        broken credentials): post ONE error row and mark every message waiting in this
        burst's stream as 'error' so the wind-down re-drive does not restart the backend
        straight away (hot loop). A later submit starts a fresh burst."""
        async with self._lock:
            stream.request_close()
            if self._stream is stream:
                self._stream = None  # a submit from here on starts a fresh burst
            ids = stream.pending_ids()
            for mid in ids:
                stream.drop(mid)
                await self._messages.set_status(mid, "error")
                await self._publish(
                    MessageDeltaFrame(project_id=self.pid, message_id=mid, status_patch="error")
                )
            await self._insert(
                role="system",
                kind="error",
                content=f"The agent could not start: {self._safe_reason(exc)}",
                metadata={"code": "agent_unavailable"},
            )

    def _safe_reason(self, exc: Exception) -> str:
        """A short, single-line reason with every configured secret redacted."""
        text = (str(exc).strip().splitlines() or [""])[0] or type(exc).__name__
        s = self._settings
        for secret in (
            s.claude_code_oauth_token,
            s.anthropic_api_key,
            s.admin_password,
            s.session_secret,
        ):
            value = secret.get_secret_value() if secret is not None else ""
            if len(value) >= 4:
                text = text.replace(value, "***")
        return text[:200]

    async def _await_wind_down(self, prev: asyncio.Task[None]) -> None:
        """Wait (bounded by the grace period) for the previous burst's backend to return;
        cancel it if it does not, so later messages never stay queued indefinitely."""
        try:
            done, _ = await asyncio.wait({prev}, timeout=self._grace)
            if not done:
                log.warning(
                    "previous burst for %s did not end in %.1fs; cancelling it",
                    self.pid,
                    self._grace,
                )
                prev.cancel()
                await asyncio.wait({prev}, timeout=self._grace)
        except asyncio.CancelledError:
            prev.cancel()
            raise

    async def _gated(self, stream: PushStream[QueuedItem]) -> AsyncIterator[QueuedItem]:
        async for item in stream:
            self._inflight = item.message_id
            yield await self._begin_turn(item)

    async def _begin_turn(self, item: QueuedItem) -> QueuedItem:
        run_id = str(uuid.uuid4())
        row = await self._messages.get(item.message_id)
        info = TurnInfo(
            run_id=run_id,
            message_id=item.message_id,
            text=item.text,
            metadata=row.metadata if row else {},
        )
        async with self._lock:
            self._turn = _Turn(info)
            await self._runs.start(run_id, self.pid)
            await self._messages.assign_run(item.message_id, run_id, "done")
            patch: dict[str, Any] = {"runId": run_id}
            if info.metadata.get("rateLimited"):
                # The parked message runs again: it no longer waits for the limit.
                await self._messages.merge_metadata(item.message_id, {"rateLimited": False})
                patch["rateLimited"] = False
            await self._publish(
                MessageDeltaFrame(
                    project_id=self.pid,
                    message_id=item.message_id,
                    status_patch="done",
                    metadata_patch=patch,
                )
            )
            await self._publish(RunStartedFrame(project_id=self.pid, run_id=run_id))
        try:
            await self.hooks.on_turn_start(info)
        except Exception:
            log.exception("on_turn_start hook failed")
        notes, self._notes = self._notes, []
        if not notes:
            return item
        prefix = "\n".join(f"[CopperChat notice] {n}" for n in notes)
        return replace(item, text=f"{prefix}\n\n{item.text}")

    # ---- events
    async def _on_event(self, ev: AgentEvent, stream: PushStream[QueuedItem]) -> None:
        if isinstance(ev, SessionId):
            await self._agent_sessions.set(self.pid, ev.id)
            return
        if isinstance(ev, TurnFinished) and ev.unsolicited:
            # End of a turn the CLI started on its own (background task notification,
            # X20 (3)): it answers no queued item, so it must neither end the current
            # turn nor open the gate for the next item.
            log.warning("ignoring unsolicited turn end for %s: %r", self.pid, ev)
            return
        turn = self._turn
        if turn is None:
            # Includes a TurnFinished with no item in flight: never release on it.
            log.debug("ignoring %r outside a turn", ev)
            return
        if isinstance(ev, TurnFinished):
            await self._turn_finished(turn, ev, stream)
            return
        async with self._lock:
            if isinstance(ev, SubagentText) or (
                isinstance(ev, ToolUse | ToolResultEvt) and ev.parent_tool_use_id
            ):
                # Keyed purely off parent_tool_use_id: complete rows that never append to
                # or close the main agent's streaming segment.
                await self._insert_subagent(ev)
            elif isinstance(ev, TextDelta):
                await self._append(turn, "text", ev.text)
            elif isinstance(ev, ThinkingDelta):
                await self._append(turn, "thinking", ev.text)
            elif isinstance(ev, ToolUse):
                await self._close_segment(turn)
                name = ev.name.removeprefix(f"mcp__{SERVER_NAME}__")
                await self._insert(
                    role="assistant",
                    kind="tool_use",
                    content=name,
                    metadata={"toolUseId": ev.id, "name": name, "input": ev.input},
                )
            elif isinstance(ev, ToolResultEvt):
                await self._close_segment(turn)
                urls = [self._store_image(img) for img in ev.images]
                await self._insert(
                    role="assistant",
                    kind="tool_result",
                    content=ev.content,
                    metadata={
                        "toolUseId": ev.id,
                        "isError": ev.is_error,
                        "images": [{"url": u} for u in urls],
                    },
                )
                for url in urls:
                    await self._insert(
                        role="assistant",
                        kind="image",
                        content="",
                        metadata={"toolUseId": ev.id, "url": url},
                    )
            elif isinstance(ev, Usage):
                payload = UsagePayload(
                    input_tokens=ev.input_tokens,
                    output_tokens=ev.output_tokens,
                    cache_read_tokens=ev.cache_read_tokens,
                    cache_creation_tokens=ev.cache_creation_tokens,
                    cost_usd=ev.cost_usd,
                    rate_limit=RateLimitPayload(
                        status=ev.rate_limit_status,
                        resets_at=ev.rate_limit_resets_at.isoformat()
                        if ev.rate_limit_resets_at
                        else None,
                    )
                    if ev.rate_limit_status
                    else None,
                )
                turn.usage = payload.model_dump(mode="json", by_alias=True)
                await self._publish(UsageFrame(project_id=self.pid, usage=payload))
            elif isinstance(ev, RateLimited):
                turn.rate_limited = ev.resets_at
        if (turn.kicad_failed or turn.document_inconsistent) and not turn.kicad_reported:
            turn.kicad_reported = True
            if turn.kicad_failed:
                content = (
                    "KiCad stopped responding; the KiCad API server was restarted and "
                    "this turn ended. Send your message again to retry."
                )
                code = "kicad_unavailable"
            else:
                content = (
                    "A failed edit could not be undone exactly, so this turn ended. The "
                    "design is reloaded from its last save."
                )
                code = "document_inconsistent"
            async with self._lock:
                await self._insert(
                    role="system", kind="error", content=content, metadata={"code": code}
                )
            try:
                await self.backend.interrupt()
            except Exception:  # the error row is posted; never add a second one (X18)
                log.exception("backend.interrupt() after KiCad failure failed for %s", self.pid)

    async def _turn_finished(
        self, turn: _Turn, ev: TurnFinished, stream: PushStream[QueuedItem]
    ) -> None:
        if (resets_at := self._park_until(turn)) is not None:
            await self._park(turn, stream, resets_at)
            return
        if ev.reason == "error" and ev.error:
            async with self._lock:
                await self._insert(role="system", kind="error", content=ev.error)
        failed = turn.kicad_failed or turn.document_inconsistent
        await self._finish_turn("error" if failed else ev.reason)
        stream.release_next()
        if stream.pending_count() == 0:
            stream.request_close()

    # ---- rate-limit parking
    def _park_until(self, turn: _Turn) -> datetime | None:
        """A turn that saw a rejected rate limit is parked and re-run later, unless the
        user stopped it: then it ends as interrupted like any other stopped turn."""
        return None if self._interrupting else turn.rate_limited

    async def _park(self, turn: _Turn, stream: PushStream[QueuedItem], resets_at: datetime) -> None:
        """End the turn as rate limited: its message and every waiting one stay 'queued'
        (outside the stream) until the limit resets; then they run again in order."""
        # Set first: from here on neither submit_ex nor the burst's re-drive enqueues.
        self.rate_limited_until = resets_at
        async with self._lock:
            for mid in stream.pending_ids():
                stream.drop(mid)  # they stay 'queued' in the DB
            mid = turn.info.message_id
            await self._messages.set_status(mid, "queued")
            await self._messages.merge_metadata(mid, {"rateLimited": True})
            await self._publish(
                MessageDeltaFrame(
                    project_id=self.pid,
                    message_id=mid,
                    status_patch="queued",
                    metadata_patch={"rateLimited": True},
                )
            )
        stream.request_close()
        stream.release_next()
        await self._finish_turn("rate_limited")
        await self._rate_limits.set(self.pid, resets_at)
        async with self._lock:
            await self._publish(
                RateLimitedFrame(project_id=self.pid, resets_at=resets_at.isoformat())
            )
        self._arm_resume(resets_at)

    def _arm_resume(self, resets_at: datetime) -> None:
        if self._resume_timer is not None:
            self._resume_timer.cancel()
        delay = max(0.0, (resets_at - datetime.now(UTC)).total_seconds())
        self._resume_timer = asyncio.get_running_loop().call_later(
            delay, lambda: self._spawn(self._resume_after_limit())
        )

    async def _resume_after_limit(self) -> None:
        self._resume_timer = None
        if self._closed:
            return
        self.rate_limited_until = None
        await self._rate_limits.set(self.pid, None)
        await self._drive_queued()

    async def _finish_turn(self, reason: FinishReason) -> None:
        turn = self._turn
        if turn is None:
            return
        async with self._lock:
            await self._close_segment(turn)
            await self._runs.finish(turn.info.run_id, reason, turn.usage)
            await self._publish(
                RunFinishedFrame(project_id=self.pid, run_id=turn.info.run_id, reason=reason)
            )
            self._turn = None
            self._inflight = None
            self._interrupting = False  # an interrupt targets exactly one turn
        outcome = TurnOutcome(
            reason=reason,
            changed_docs=frozenset(turn.changed),
            mutated=turn.mutated,
            kicad_failed=turn.kicad_failed,
            document_inconsistent=turn.document_inconsistent,
            auto_restore=turn.auto_restore,
        )
        try:
            await self.hooks.on_turn_end(turn.info, outcome)
        except Exception:
            log.exception("on_turn_end hook failed")

    # ---- persistence helpers (caller holds self._lock)
    async def _cancel_row(self, message_id: int) -> None:
        await self._messages.set_status(message_id, "cancelled")
        await self._publish(
            MessageDeltaFrame(project_id=self.pid, message_id=message_id, status_patch="cancelled")
        )

    async def _publish(self, frame: Wire) -> None:
        await self.broadcaster.publish(self.pid, frame)

    async def _insert(
        self,
        *,
        role: str,
        kind: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        status: str = "done",
    ) -> MessageRow:
        mid = await self._messages.insert(
            self.pid,
            role=role,
            kind=kind,
            content=content,
            status=status,
            run_id=self.current_run_id,
            metadata=metadata,
        )
        row = await self._messages.get(mid)
        if row is None:
            raise RuntimeError(f"message {mid} vanished after insert")
        await self._publish(MessageFrame(project_id=self.pid, message=self.to_dto(row)))
        return row

    async def _insert_subagent(self, ev: SubagentText | ToolUse | ToolResultEvt) -> None:
        parent = ev.parent_tool_use_id
        if isinstance(ev, SubagentText):
            await self._insert(
                role="assistant",
                kind="text",
                content=ev.text,
                metadata={"parentToolUseId": parent},
            )
        elif isinstance(ev, ToolUse):
            name = ev.name.removeprefix(f"mcp__{SERVER_NAME}__")
            await self._insert(
                role="assistant",
                kind="tool_use",
                content=name,
                metadata={
                    "parentToolUseId": parent,
                    "toolUseId": ev.id,
                    "name": name,
                    "input": _clip(ev.input),
                },
            )
        else:  # images of sub-agent results are dropped
            await self._insert(
                role="assistant",
                kind="tool_result",
                content=ev.content[:_SUB_RESULT_MAX],
                metadata={
                    "parentToolUseId": parent,
                    "toolUseId": ev.id,
                    "isError": ev.is_error,
                    "images": [],
                },
            )

    async def _append(self, turn: _Turn, kind: str, text: str) -> None:
        seg = turn.seg
        if seg is not None and seg.kind != kind:
            await self._close_segment(turn)
            seg = None
        if seg is None:
            row = await self._insert(role="assistant", kind=kind, content="", status="streaming")
            seg = turn.seg = _Segment(id=row.id, kind=kind, last_flush=time.monotonic())
        seg.content += text
        self._live[seg.id] = seg.content
        await self._publish(
            MessageDeltaFrame(project_id=self.pid, message_id=seg.id, content_delta=text)
        )
        if (
            len(seg.content) - seg.flushed_len >= self._flush_chars
            or time.monotonic() - seg.last_flush >= self._flush_interval
        ):
            await self._flush(seg)

    async def _flush(self, seg: _Segment) -> None:
        await self._messages.set_content(seg.id, seg.content)
        seg.flushed_len = len(seg.content)
        seg.last_flush = time.monotonic()

    async def _close_segment(self, turn: _Turn) -> None:
        seg = turn.seg
        if seg is None:
            return
        await self._flush(seg)
        await self._messages.set_status(seg.id, "done")
        self._live.pop(seg.id, None)
        turn.seg = None
        await self._publish(
            MessageDeltaFrame(project_id=self.pid, message_id=seg.id, status_patch="done")
        )

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            async with self._lock:
                turn = self._turn
                if turn and turn.seg and len(turn.seg.content) > turn.seg.flushed_len:
                    await self._flush(turn.seg)

    def _store_image(self, img: ImageBlock) -> str:
        name = f"{uuid.uuid4().hex}.{_IMAGE_EXT.get(img.mime, 'bin')}"
        d = agent_dir(self.project) / "images"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(img.data)
        return f"/api/projects/{self.pid}/images/{name}"

    # ---- tool context
    def _make_ctx(self) -> ToolContext:
        session = self._sessions.current()
        if session is None or session.closed or session.project.id != self.pid:
            raise KicadUnavailable("the project is not open in KiCad")
        turn = self._turn
        return ToolContext(
            project=self.project,
            session=session,
            settings=self._settings,
            run_id=turn.info.run_id if turn else None,
            on_mutation=self._on_mutation,
            user_message=turn.info.text if turn else None,
            on_documents_changed=self._on_documents_changed,
            on_kicad_unavailable=self._on_kicad_unavailable,
            editing=self.editing,
            on_document_inconsistent=self._on_document_inconsistent,
        )

    async def _on_mutation(self) -> None:
        turn = self._turn
        if turn is not None and not turn.mutated:
            # Marked only once the hook succeeded: if the undo snapshot failed, that call
            # fails and the next mutating call of the turn tries the snapshot again.
            await self.hooks.on_first_mutation(turn.info)
            turn.mutated = True

    def _on_documents_changed(self, docs: set[DocKind]) -> None:
        # X2: Phase 5 never publishes view-updated; Phase 7 does it from TurnOutcome.
        if self._turn is not None:
            self._turn.changed |= docs

    def _on_kicad_unavailable(self, exc: Exception) -> None:
        log.warning("KiCad unavailable during turn: %s", exc)
        if self._turn is not None:
            self._turn.kicad_failed = True  # reported + interrupted after the tool result

    def _on_document_inconsistent(self, exc: Exception) -> None:
        log.warning("document inconsistent during turn: %s", exc)
        if self._turn is not None:
            self._turn.document_inconsistent = True  # reported + interrupted like kicad_failed
            self._turn.auto_restore |= bool(getattr(exc, "auto_restore", False))

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task
