"""Per-project run engine: queue-first persistence, gated input, streaming, hooks."""

from __future__ import annotations

import asyncio
import itertools
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
    ConversationDTO,
    ConversationUpdatedFrame,
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
from copperchat.db import Database, utcnow_iso
from copperchat.db.repos.agent_state import RateLimitRepo, RunRepo
from copperchat.db.repos.conversations import ConversationRepo, ConversationRow
from copperchat.db.repos.messages import MessageRepo, MessageRow
from copperchat.errors import ConversationClosed, ConversationNotFound, CopperChatError
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
NOTICE_PREFIX = "[CopperChat notice]"
# Broadcast agent notes kept for conversations that have not run a turn since (oldest
# dropped first): they are about the shared design state, so only recent ones matter.
_BROADCAST_KEEP = 100


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
    conversation_id: str


@dataclass(frozen=True)
class TurnOutcome:
    reason: FinishReason
    # The turn's conversation and the final status of the message that started it
    # ('queued' again when the turn was parked by a rate limit) (S-C3).
    conversation_id: str | None = None
    message_status: str = "done"
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

    async def before_model_input(self, turn: TurnInfo) -> list[str]:
        """Extra notices for the model input of this turn (C5 context injection). Each
        entry is one notice: it gets exactly one ``[CopperChat notice]`` prefix at its
        start, even if it spans several lines. Never part of ``TurnInfo.text``."""
        return []

    async def on_items_failed(
        self, conversation_id: str, message_ids: list[int], status: str
    ) -> None:
        """Waiting user messages of ``conversation_id`` ended without a turn: cancelled
        (Stop, cancel-queued) or ``error`` (the backend could not start)."""
        return None


class ChainedHooks(RunHooks):
    """Several hooks in order (history first, E-C5). A failing start/mutation/input hook
    stops the chain and propagates (a failed snapshot must fail the tool call); turn-end
    and items-failed hooks all run, then the first error propagates."""

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

    async def before_model_input(self, turn: TurnInfo) -> list[str]:
        out: list[str] = []
        for h in self.hooks:
            out.extend(await h.before_model_input(turn))
        return out

    async def on_items_failed(
        self, conversation_id: str, message_ids: list[int], status: str
    ) -> None:
        first: Exception | None = None
        for h in self.hooks:
            try:
                await h.on_items_failed(conversation_id, message_ids, status)
            except Exception as exc:
                first = first or exc
        if first is not None:
            raise first


# (tool context factory, turn context = the running turn's run id) -> backend (S-C3).
BackendFactory = Callable[[Callable[[], ToolContext], Callable[[], str | None]], AgentBackend]


async def conversation_dto(repo: ConversationRepo, row: ConversationRow) -> ConversationDTO:
    return ConversationDTO.model_validate(row.to_wire(await repo.usage_for(row.id)))


async def publish_conversation(
    broadcaster: Broadcaster, repo: ConversationRepo, row: ConversationRow
) -> ConversationDTO:
    """``conversation-updated`` for ``row`` (create, rename, archive, status, activity)."""
    dto = await conversation_dto(repo, row)
    await broadcaster.publish(
        row.project_id, ConversationUpdatedFrame(project_id=row.project_id, conversation=dto)
    )
    return dto


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
        self._conversations = ConversationRepo(db)
        self._rate_limits = RateLimitRepo(db)
        self._flush_interval = flush_interval_s
        self._flush_chars = flush_chars
        self._grace = interrupt_grace_s
        self._lock = asyncio.Lock()  # serialises persist+publish against replay snapshots
        self._stream: PushStream[QueuedItem] | None = None
        # Set by seal() until the burst owning that stream has ended: while set, every new
        # item stays DB-queued so FIFO order across conversations holds (spec §5.2).
        self._sealed_stream: PushStream[QueuedItem] | None = None
        self._burst: asyncio.Task[None] | None = None  # newest burst
        self._bursts: set[asyncio.Task[None]] = set()  # incl. ones still winding down
        self._turn: _Turn | None = None
        self._inflight: int | None = None  # pulled from the stream, turn not finished
        self._inflight_cid: str | None = None  # its conversation (set with _inflight)
        self._live: dict[int, str] = {}  # streaming row id -> full content incl. unflushed
        # Agent notes (spec §5.3): per conversation, plus broadcast notes about the shared
        # design state that each conversation sees once, at its next turn.
        self._notes: dict[str, list[str]] = {}
        self._broadcast: list[tuple[int, str, str]] = []  # (id, created at, text)
        self._broadcast_ids = itertools.count(1)
        self._delivered: dict[str, set[int]] = {}
        self._active: set[str] = set()  # conversations known to have messages
        self._interrupting = False
        self._closed = False
        self._resume_timer: asyncio.TimerHandle | None = None
        self._bg: set[asyncio.Task[Any]] = set()
        self.backend: AgentBackend = backend_factory(self._make_ctx, self.turn_context)

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

    @property
    def current_conversation_id(self) -> str | None:
        return self._turn.info.conversation_id if self._turn else None

    def turn_context(self) -> str | None:
        """The running turn's run id; KiCad-less tool contexts carry it (S-C3)."""
        return self.current_run_id

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
        self,
        conversation_id: str,
        client_message_id: str,
        text: str,
        attachments: list[AttachmentRef],
    ) -> MessageDTO:
        dto, _ = await self.submit_ex(conversation_id, client_message_id, text, attachments)
        return dto

    async def submit_ex(
        self,
        conversation_id: str,
        client_message_id: str,
        text: str,
        attachments: list[AttachmentRef],
        metadata: dict[str, Any] | None = None,
    ) -> tuple[MessageDTO, bool]:
        """Queue a user message into ``conversation_id``. ConversationNotFound unless it is
        one of this project's; ConversationClosed unless it is open (the close-session
        handover turn may be sent while it is closing)."""
        conv = await self._conversation(conversation_id)
        origin = (metadata or {}).get("origin")
        if conv.status != "open" and not (conv.status == "closing" and origin == "close-session"):
            raise ConversationClosed(f"{conv.title} is {conv.status}; reopen it to continue")
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
            first = await self._first_message(conversation_id)
            row, created = await self._messages.insert_user(
                self.pid, conversation_id, client_message_id, text, meta
            )
            dto = self.to_dto(row)
            await self._publish(MessageFrame(project_id=self.pid, message=dto))
            if created and first:
                await self._publish_conversation(conversation_id)
            # Enqueue under the lock: _drive_queued scans under it too, so a 'queued' row
            # is always either in the stream, in flight, or not yet visible to the scan.
            if created and self.rate_limited_until is None:
                self._enqueue(self._item(row))
        return dto, created

    async def post_notice(
        self, conversation_id: str, text: str, metadata: dict[str, Any] | None = None
    ) -> MessageDTO:
        await self._conversation(conversation_id)
        async with self._lock:
            row = await self._insert(
                conversation_id=conversation_id,
                role="system",
                kind="notice",
                content=text,
                metadata=metadata,
            )
        return self.to_dto(row)

    async def post_error(
        self, conversation_id: str, text: str, metadata: dict[str, Any] | None = None
    ) -> MessageDTO:
        await self._conversation(conversation_id)
        async with self._lock:
            row = await self._insert(
                conversation_id=conversation_id,
                role="system",
                kind="error",
                content=text,
                metadata=metadata,
            )
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
            await self._cancel_row(message_id, row.conversation_id)
        await self._items_failed(row.conversation_id, [message_id], "cancelled")
        return True

    async def interrupt(self, run_id: str | None, conversation_id: str | None = None) -> None:
        """Stop: end the running turn and cancel the waiting messages of ONE conversation
        (spec §5.2): the running turn's, else (between turns) ``conversation_id``, the
        session the user pressed Stop for. Other conversations' messages stay queued.

        A Stop for an older run (``run_id`` no longer current) is ignored. If the backend
        does not end the interrupted turn within the grace period, the burst is cancelled
        (backstop); a message sent after the Stop is never hit by that backstop.
        """
        async with self._lock:
            # Decided under the lock: an item pulled from the stream is in flight before its
            # turn (and run id) exists.
            current = self.current_run_id
            if (
                run_id is not None
                and run_id != current
                and (current is not None or self._inflight is not None)
            ):
                return  # stale Stop for an older run
            target_cid = self.current_conversation_id or conversation_id
            stream = self._stream
            own = (
                stream is not None
                and target_cid is not None
                and stream.conversation_id == target_cid
            )
            ids: list[int] = []
            if own and stream is not None:
                ids = stream.pending_ids()
                for mid in ids:
                    stream.drop(mid)
            if target_cid is not None:
                # DB-queued rows of the conversation (sealed out of the stream, parked).
                ids += [
                    r.id
                    for r in await self._messages.queued_user_rows(self.pid)
                    if r.conversation_id == target_cid
                    and r.id != self._inflight
                    and r.id not in ids
                ]
                for mid in ids:
                    await self._cancel_row(mid, target_cid)
            # Only the target conversation's item is interrupted: a Stop for B never hits
            # A's item that is pulled but whose turn has not started yet.
            target = self._inflight if self._inflight_cid == target_cid else None
            if target is not None:
                self._interrupting = True
        if ids and target_cid is not None:
            await self._items_failed(target_cid, sorted(ids), "cancelled")
        task = self._burst
        if task is None or task.done():
            return
        if own and stream is not None:
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

    def add_agent_note(self, text: str, conversation_id: str | None = None) -> None:
        """A note prefixed to the next model input of ``conversation_id``; None broadcasts
        it to every open conversation, each sees it once at its next turn (spec §5.3)."""
        if conversation_id is not None:
            self._notes.setdefault(conversation_id, []).append(text)
            return
        self._broadcast.append((next(self._broadcast_ids), utcnow_iso(), text))
        del self._broadcast[:-_BROADCAST_KEEP]

    def _take_notes(self, conversation: ConversationRow) -> list[str]:
        cid = conversation.id
        delivered = self._delivered.setdefault(cid, set())
        out: list[str] = []
        for nid, at, text in self._broadcast:
            # A session opened after the note never saw the state it is about.
            if nid not in delivered and at >= conversation.created_at:
                out.append(text)
            delivered.add(nid)
        delivered &= {nid for nid, _, _ in self._broadcast}
        out.extend(self._notes.pop(cid, []))
        return out

    async def replay_and_subscribe(
        self, conversation_id: str, since_message_id: int
    ) -> tuple[list[MessageDTO], Subscription, str | None, str | None]:
        """Snapshot of one conversation + a subscription to all of the project's live
        frames, atomically w.r.t. publishes: no gap, no duplicate delta. Also returns the
        running turn's run and conversation ids."""
        async with self._lock:
            rows = await self._messages.since(self.pid, conversation_id, since_message_id)
            dtos = [self.to_dto(r) for r in rows]
            sub = self.broadcaster.subscribe(self.pid)
            return dtos, sub, self.current_run_id, self.current_conversation_id

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
            conversation_id=row.conversation_id,
            text=row.content,
            attachments=atts,
        )

    def _enqueue(self, item: QueuedItem) -> None:
        """Push into the open stream when the item belongs to its conversation; a foreign
        item seals that stream and stays DB-queued, like every item while a sealed
        stream's burst runs. The burst's end re-drives them in id order (spec §5.2)."""
        if self._sealed_stream is not None:
            return
        current = self._stream
        if current is not None and current.conversation_id != item.conversation_id:
            current.seal()
            self._sealed_stream = current
            return
        if current is not None and current.push(item):
            return
        stream: PushStream[QueuedItem] = PushStream(item.conversation_id)
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
            # The burst's conversation and its resume id: read once per burst, never
            # mid-burst.
            conversation = await self._conversation(stream.conversation_id)
            resume = conversation.sdk_session_id
            flusher = asyncio.create_task(self._flush_loop())
            async for ev in self.backend.start(
                self.project, conversation, self._gated(stream, conversation), resume
            ):
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
            if self._sealed_stream is stream:  # never an older burst unsealing a newer one
                self._sealed_stream = None
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
            cid = stream.conversation_id
            for mid in ids:
                stream.drop(mid)
                await self._messages.set_status(mid, "error")
                await self._publish(
                    MessageDeltaFrame(
                        project_id=self.pid,
                        conversation_id=cid,
                        message_id=mid,
                        status_patch="error",
                    )
                )
            await self._insert(
                conversation_id=cid,
                role="system",
                kind="error",
                content=f"The agent could not start: {self._safe_reason(exc)}",
                metadata={"code": "agent_unavailable"},
            )
        if ids:
            await self._items_failed(cid, ids, "error")

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

    async def _gated(
        self, stream: PushStream[QueuedItem], conversation: ConversationRow
    ) -> AsyncIterator[QueuedItem]:
        async for item in stream:
            self._inflight = item.message_id
            self._inflight_cid = item.conversation_id
            yield await self._begin_turn(item, conversation)

    async def _begin_turn(self, item: QueuedItem, conversation: ConversationRow) -> QueuedItem:
        run_id = str(uuid.uuid4())
        cid = item.conversation_id
        row = await self._messages.get(item.message_id)
        info = TurnInfo(
            run_id=run_id,
            message_id=item.message_id,
            text=item.text,
            metadata=row.metadata if row else {},
            conversation_id=cid,
        )
        async with self._lock:
            self._turn = _Turn(info)
            await self._runs.start(run_id, self.pid, cid)
            await self._messages.assign_run(item.message_id, run_id, "done")
            patch: dict[str, Any] = {"runId": run_id}
            if info.metadata.get("rateLimited"):
                # The parked message runs again: it no longer waits for the limit.
                await self._messages.merge_metadata(item.message_id, {"rateLimited": False})
                patch["rateLimited"] = False
            await self._publish(
                MessageDeltaFrame(
                    project_id=self.pid,
                    conversation_id=cid,
                    message_id=item.message_id,
                    status_patch="done",
                    metadata_patch=patch,
                )
            )
            await self._publish(
                RunStartedFrame(project_id=self.pid, conversation_id=cid, run_id=run_id)
            )
        try:
            await self.hooks.on_turn_start(info)
        except Exception:
            log.exception("on_turn_start hook failed")
        notices = self._take_notes(conversation)
        try:
            notices += await self.hooks.before_model_input(info)
        except Exception:  # the turn continues without the injected notices
            log.exception("before_model_input hook failed")
        if not notices:
            return item
        # One prefix per notice, at its start; lines inside a notice are repository data
        # and never get a prefix of their own (S-C3, spec §9.1).
        prefix = "\n".join(f"{NOTICE_PREFIX} {n}" for n in notices)
        return replace(item, text=f"{prefix}\n\n{item.text}")

    # ---- events
    async def _on_event(self, ev: AgentEvent, stream: PushStream[QueuedItem]) -> None:
        if isinstance(ev, SessionId):
            await self._conversations.set_sdk_session(stream.conversation_id, ev.id)
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
                await self._publish(
                    UsageFrame(
                        project_id=self.pid,
                        conversation_id=turn.info.conversation_id,
                        usage=payload,
                    )
                )
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
                    "A failed edit could not be undone exactly, so this turn ended. "
                    "CopperChat reloads the design (see the notice below)."
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
                    conversation_id=turn.info.conversation_id,
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
            # The session list shows the summed usage and the last activity.
            await self._publish_conversation(turn.info.conversation_id)
            await self._publish(
                RunFinishedFrame(
                    project_id=self.pid,
                    conversation_id=turn.info.conversation_id,
                    run_id=turn.info.run_id,
                    reason=reason,
                )
            )
            self._turn = None
            self._inflight = None
            self._inflight_cid = None
            self._interrupting = False  # an interrupt targets exactly one turn
        outcome = TurnOutcome(
            reason=reason,
            conversation_id=turn.info.conversation_id,
            message_status="queued" if reason == "rate_limited" else "done",
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

    # ---- conversations
    async def _conversation(self, conversation_id: str) -> ConversationRow:
        row = await self._conversations.get(conversation_id)
        if row is None or row.project_id != self.pid:
            raise ConversationNotFound("no such session in this project")
        return row

    async def _first_message(self, conversation_id: str) -> bool:
        """True if the conversation has no message yet (its next insert is the first)."""
        if conversation_id in self._active:
            return False
        self._active.add(conversation_id)
        row = await self._conversations.get(conversation_id)
        return row is not None and row.last_message_at is None

    async def _publish_conversation(self, conversation_id: str) -> None:
        row = await self._conversations.get(conversation_id)
        if row is None:
            return
        try:
            await publish_conversation(self.broadcaster, self._conversations, row)
        except Exception:  # the list catches up on its next load
            log.exception("conversation-updated for %s failed", conversation_id)

    async def _items_failed(self, conversation_id: str, ids: list[int], status: str) -> None:
        try:
            await self.hooks.on_items_failed(conversation_id, ids, status)
        except Exception:
            log.exception("on_items_failed hook failed")

    # ---- persistence helpers (caller holds self._lock)
    async def _cancel_row(self, message_id: int, conversation_id: str) -> None:
        await self._messages.set_status(message_id, "cancelled")
        await self._publish(
            MessageDeltaFrame(
                project_id=self.pid,
                conversation_id=conversation_id,
                message_id=message_id,
                status_patch="cancelled",
            )
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
        conversation_id: str | None = None,
    ) -> MessageRow:
        """A row in ``conversation_id`` (default: the running turn's). It belongs to the
        running turn's run only if it is in that turn's conversation."""
        turn_cid = self.current_conversation_id
        cid = conversation_id or turn_cid
        if cid is None:
            raise RuntimeError("a message outside a turn needs a conversation")
        first = await self._first_message(cid)
        mid = await self._messages.insert(
            self.pid,
            cid,
            role=role,
            kind=kind,
            content=content,
            status=status,
            run_id=self.current_run_id if cid == turn_cid else None,
            metadata=metadata,
        )
        row = await self._messages.get(mid)
        if row is None:
            raise RuntimeError(f"message {mid} vanished after insert")
        await self._publish(MessageFrame(project_id=self.pid, message=self.to_dto(row)))
        if first:
            await self._publish_conversation(cid)
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
            MessageDeltaFrame(
                project_id=self.pid,
                conversation_id=turn.info.conversation_id,
                message_id=seg.id,
                content_delta=text,
            )
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
            MessageDeltaFrame(
                project_id=self.pid,
                conversation_id=turn.info.conversation_id,
                message_id=seg.id,
                status_patch="done",
            )
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
            if self.editing is not None:  # every report halts further edits of the turn
                self.editing.turn(self.pid, self._turn.info.run_id).halted = True
            self._turn.auto_restore |= bool(getattr(exc, "auto_restore", False))

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task
