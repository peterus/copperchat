"""Per-project run engine: queue-first persistence, gated input, streaming, hooks."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from copperchat.agent.broadcaster import Broadcaster, Subscription
from copperchat.agent.events import (
    AgentBackend,
    AgentEvent,
    QueuedItem,
    RateLimited,
    SessionId,
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

log = logging.getLogger(__name__)
_IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


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
        self.attachment_resolver: Callable[[str], AttachmentRef | None] = lambda _id: None
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
        self._burst: asyncio.Task[None] | None = None
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
        await self.hooks.on_engine_start()
        await self._restore()

    async def close(self) -> None:
        self._closed = True
        if self._resume_timer is not None:
            self._resume_timer.cancel()
        task = self._burst
        if task is not None and not task.done():
            task.cancel()
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
        for att in attachments:
            if self.attachment_resolver(att.id) is None:
                raise UnknownAttachment(f"unknown attachment: {att.id}")
        meta = dict(metadata or {})
        if attachments:
            meta["attachments"] = [a.model_dump(mode="json", by_alias=True) for a in attachments]
        async with self._lock:
            row, created = await self._messages.insert_user(self.pid, client_message_id, text, meta)
            dto = self.to_dto(row)
            await self._publish(MessageFrame(project_id=self.pid, message=dto))
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
        self._burst = asyncio.create_task(self._run_burst(stream, prev), name=f"burst-{self.pid}")

    async def _restore(self) -> None:
        await self._drive_queued()

    async def _drive_queued(self) -> None:
        if self._closed or self.rate_limited_until is not None:
            return
        known = set(self._stream.pending_ids()) if self._stream else set()
        for row in await self._messages.queued_user_rows(self.pid):
            if row.id not in known and row.id != self._inflight:
                self._enqueue(self._item(row))

    async def _run_burst(
        self, stream: PushStream[QueuedItem], prev: asyncio.Task[None] | None = None
    ) -> None:
        if prev is not None:
            await asyncio.wait({prev})
        resume = await self._agent_sessions.get(self.pid)  # read once per burst, never mid-burst
        flusher = asyncio.create_task(self._flush_loop())
        try:
            async for ev in self.backend.start(self.project, self._gated(stream), resume):
                await self._on_event(ev, stream)
        except asyncio.CancelledError:
            if not self._interrupting:
                raise
            log.warning("burst for %s cancelled by the interrupt backstop", self.pid)
        except Exception as exc:
            log.exception("agent backend failed for %s", self.pid)
            if self._turn is not None:
                async with self._lock:
                    await self._insert(
                        role="system",
                        kind="error",
                        content=f"The agent stopped unexpectedly: {exc}",
                    )
                await self._finish_turn("error")
        finally:
            flusher.cancel()
            if self._turn is not None:
                await self._finish_turn(
                    "interrupted" if (self._interrupting or self._closed) else "error"
                )
            if self._stream is stream:
                self._stream = None
            if self._burst is asyncio.current_task():
                self._burst = None
            self._interrupting = False
            await self._drive_queued()

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
            await self._publish(
                MessageDeltaFrame(
                    project_id=self.pid,
                    message_id=item.message_id,
                    status_patch="done",
                    metadata_patch={"runId": run_id},
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
            if isinstance(ev, TextDelta):
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
        if turn.kicad_failed and not turn.kicad_reported:
            turn.kicad_reported = True
            async with self._lock:
                await self._insert(
                    role="system",
                    kind="error",
                    content="KiCad stopped responding; the KiCad API server was restarted and "
                    "this turn ended. Send your message again to retry.",
                    metadata={"code": "kicad_unavailable"},
                )
            await self.backend.interrupt()

    async def _turn_finished(
        self, turn: _Turn, ev: TurnFinished, stream: PushStream[QueuedItem]
    ) -> None:
        if ev.reason == "error" and ev.error:
            async with self._lock:
                await self._insert(role="system", kind="error", content=ev.error)
        await self._finish_turn("error" if turn.kicad_failed else ev.reason)
        stream.release_next()
        if stream.pending_count() == 0:
            stream.request_close()

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
        outcome = TurnOutcome(
            reason=reason,
            changed_docs=frozenset(turn.changed),
            mutated=turn.mutated,
            kicad_failed=turn.kicad_failed,
        )
        try:
            await self.hooks.on_turn_end(turn.info, outcome)
        except Exception:
            log.exception("on_turn_end hook failed")

    # ---- persistence helpers (caller holds self._lock)
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
        if session is None or session.project.id != self.pid:
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
        )

    async def _on_mutation(self) -> None:
        turn = self._turn
        if turn is not None and not turn.mutated:
            turn.mutated = True
            await self.hooks.on_first_mutation(turn.info)

    def _on_documents_changed(self, docs: set[DocKind]) -> None:
        # X2: Phase 5 never publishes view-updated; Phase 7 does it from TurnOutcome.
        if self._turn is not None:
            self._turn.changed |= docs

    def _on_kicad_unavailable(self, exc: Exception) -> None:
        log.warning("KiCad unavailable during turn: %s", exc)
        if self._turn is not None:
            self._turn.kicad_failed = True  # reported + interrupted after the tool result

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task
