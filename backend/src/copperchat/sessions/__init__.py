"""Session lifecycle (sessions C5): context injection, close/reopen and the handover.

``install_sessions(app)`` runs in the lifespan after ``install_plans`` and
``install_design_docs`` and before ``resume_pending``: it hands the docs service to the
runtime (every ToolContext gets it) and chains :class:`SessionHooks` after the existing
hooks, so the engine sees them through its real ``ChainedHooks``.

Close (spec §10): ``POST …/close`` sets ``closing`` and enqueues a server-authored
message (``metadata.origin='close-session'``). Its turn ends ``completed`` → ``closed``
plus a ``decisions.md`` entry written by the server from the final assistant text; any
other end (interrupted, error, cancelled, ``_fail_burst``, a crash) → back to ``open``
with a notice; ``rate_limited`` stays ``closing`` until the parked re-run settles it.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from copperchat.agent.context_injection import inject
from copperchat.agent.engine import RunHooks, TurnInfo, TurnOutcome, publish_conversation
from copperchat.db import Database
from copperchat.db.repos.conversations import ConversationRepo, ConversationRow
from copperchat.db.repos.messages import MessageRepo
from copperchat.design_docs.paths import DocError
from copperchat.design_docs.service import ConversationInfo
from copperchat.errors import ConversationStateConflict, CopperChatError
from copperchat.plans.repo import PlanRepo
from copperchat.repo_context.paths import repo_policy

if TYPE_CHECKING:
    from fastapi import FastAPI

    from copperchat.agent.engine import RunEngine
    from copperchat.agent.runtime import AgentRuntime
    from copperchat.design_docs.service import DesignDocs
    from copperchat.projects import Project

__all__ = ["CLOSE_ORIGIN", "CLOSE_TEXT", "SessionHooks", "SessionService", "install_sessions"]

log = logging.getLogger(__name__)

CLOSE_ORIGIN = "close-session"
# Server-authored and fixed (spec §10, §13): no attachments, and no "commit" in it, so
# git_commit stays refused in the close turn.
CLOSE_TEXT = (
    "Close this session: bring the design documents up to date with what was decided and "
    "learned in this session. Use doc_patch_section / doc_append within your session's "
    'write scope. Record each decision in decisions.md. Update "Open points" in each '
    "affected doc (add new ones, remove resolved ones). Then reply with a short handover "
    "summary (≤ 150 words): what is done, what is open, what the next session should do."
)
NOT_COMPLETED = "Close did not complete — the session is open again."
PLAN_OPEN_NOTICE = (
    "Close did not complete: this session owns an open plan. Finish or abort the plan, "
    "then close the session again."
)
DECISIONS_FULL = "decisions.md is full — handover entry not recorded"
NO_SUMMARY = "The session ended without a handover summary."


class PlanOpenConflict(CopperChatError):
    """Closing a session that owns an open plan (plan index "Calls made by the author")."""

    code = "plan_open"
    http_status = 409


def _info(conv: ConversationRow) -> ConversationInfo:
    return ConversationInfo(id=conv.id, seq=conv.seq, title=conv.title, type=conv.type)


class SessionService:
    def __init__(self, db: Database, docs: DesignDocs | None, runtime: AgentRuntime) -> None:
        self.db = db
        self.docs = docs
        self.runtime = runtime
        self.conversations = ConversationRepo(db)
        self.messages = MessageRepo(db)
        self.plans = PlanRepo(db)

    async def _publish(self, row: ConversationRow) -> None:
        try:
            await publish_conversation(self.runtime.broadcaster, self.conversations, row)
        except Exception:  # the list catches up on its next load
            log.exception("conversation-updated for %s failed", row.id)

    async def _owns_open_plan(self, conv: ConversationRow) -> bool:
        owner = await self.plans.open_owner(conv.project_id)
        return owner is not None and owner[2] == conv.id

    # ---- REST
    async def close(self, project: Project, cid: str) -> ConversationRow:
        conv = await self.conversations.get_for_project(project.id, cid)
        if conv.status != "open":
            raise ConversationStateConflict(f"the session is {conv.status}")
        if await self._owns_open_plan(conv):
            raise PlanOpenConflict("this session owns an open plan: finish or abort it first")
        engine = await self.runtime.ensure(project)
        # Only one of two concurrent closes (or a close racing a reopen) gets here.
        row = await self.conversations.set_status(cid, "closing", expect="open")
        # Published before the message is queued: a fast close turn's 'closed' must not
        # be overtaken by this 'closing'.
        await self._publish(row)
        client_id = f"close-{uuid.uuid4().hex}"
        try:
            await engine.submit_ex(cid, client_id, CLOSE_TEXT, [], {"origin": CLOSE_ORIGIN})
        except BaseException:
            reverted = await self.conversations.set_status(cid, "open", expect="closing")
            await self._publish(reverted)
            # the row may be in already (a publish failed after the insert): it must not
            # run later in an open session (cancelled after the revert, so no notice)
            row_in = await self.messages.get_by_client_id(project.id, client_id)
            if row_in is not None:
                await engine.cancel_queued(row_in.id)
            raise
        return row

    async def reopen(self, project: Project, cid: str) -> ConversationRow:
        await self.conversations.get_for_project(project.id, cid)
        row = await self.conversations.set_status(cid, "open", expect="closed")
        await self._publish(row)
        return row

    # ---- lifecycle
    async def back_to_open(self, engine: RunEngine, cid: str, notice: str) -> None:
        try:
            row = await self.conversations.set_status(cid, "open", expect="closing")
        except ConversationStateConflict:
            return  # settled elsewhere
        await engine.post_notice(cid, notice, {"origin": CLOSE_ORIGIN})
        await self._publish(row)

    async def _summary(self, conversation_id: str, run_id: str) -> str:
        """The close turn's final main-agent text (its last text segment)."""
        # conversation_id first: ix_messages_conversation serves the scan
        row = await self.db.fetchone(
            "SELECT content FROM messages WHERE conversation_id = ? AND run_id = ? "
            "AND role = 'assistant' AND kind = 'text' "
            "AND json_extract(metadata, '$.parentToolUseId') IS NULL "
            "AND content != '' ORDER BY id DESC LIMIT 1",
            (conversation_id, run_id),
        )
        return str(row["content"]).strip() if row is not None else ""

    async def settle(
        self, project: Project, engine: RunEngine, turn: TurnInfo, outcome: TurnOutcome
    ) -> None:
        cid = turn.conversation_id
        conv = await self.conversations.get(cid)
        if conv is None or conv.status != "closing":
            return
        if outcome.reason == "rate_limited":
            return  # parked: settled when the re-run finishes
        if outcome.reason != "completed":
            await self.back_to_open(engine, cid, NOT_COMPLETED)
            return
        try:
            if await self._owns_open_plan(conv):
                # a plan_propose that raced the close (spec §10, plan index)
                await self.back_to_open(engine, cid, PLAN_OPEN_NOTICE)
                return
            summary = await self._summary(cid, turn.run_id)
            await self._handover_entry(project, engine, conv, summary)
            row = await self.conversations.set_status(cid, "closed", expect="closing")
        except ConversationStateConflict:
            return
        except Exception:
            # never leave the session stuck in 'closing' until the next restart
            log.exception("settling the close of %s failed", cid)
            await self.back_to_open(engine, cid, NOT_COMPLETED)
            return
        await self._publish(row)

    async def _handover_entry(
        self, project: Project, engine: RunEngine, conv: ConversationRow, summary: str
    ) -> None:
        if self.docs is None:
            return
        try:
            written = await self.docs.append_server_entry(
                project,
                f"Session {conv.seq} closed",
                summary or NO_SUMMARY,
                conversation=_info(conv),
            )
        except DocError as err:  # e.g. decisions.md unreadable: the session still closes
            await engine.post_notice(
                conv.id,
                f"The handover entry was not recorded: {err.what}",
                {"origin": CLOSE_ORIGIN},
            )
            return
        if written is None:
            await engine.post_notice(conv.id, DECISIONS_FULL, {"origin": CLOSE_ORIGIN})

    async def items_failed(self, engine: RunEngine, cid: str, message_ids: list[int]) -> None:
        conv = await self.conversations.get(cid)
        if conv is None or conv.status != "closing":
            return
        for mid in message_ids:
            row = await self.messages.get(mid)
            if row is not None and row.metadata.get("origin") == CLOSE_ORIGIN:
                await self.back_to_open(engine, cid, NOT_COMPLETED)
                return


class SessionHooks(RunHooks):
    def __init__(self, service: SessionService, project: Project, engine: RunEngine) -> None:
        self.service = service
        self.project = project
        self.engine = engine

    async def before_model_input(self, turn: TurnInfo) -> list[str]:
        svc = self.service
        if svc.docs is None:
            return []
        conv = await svc.conversations.get(turn.conversation_id)
        if conv is None:
            return []
        projects_root = svc.runtime.settings.projects_root
        result = await inject(
            svc.docs, self.project, conv, lambda: repo_policy(self.project, projects_root)
        )
        if result.docs_seen != conv.docs_seen:
            await svc.conversations.set_docs_seen(conv.id, result.docs_seen)
        return result.notices

    async def on_turn_end(self, turn: TurnInfo, outcome: TurnOutcome) -> None:
        if turn.metadata.get("origin") == CLOSE_ORIGIN:
            await self.service.settle(self.project, self.engine, turn, outcome)

    async def on_items_failed(
        self, conversation_id: str, message_ids: list[int], status: str
    ) -> None:
        await self.service.items_failed(self.engine, conversation_id, message_ids)


async def sweep_closing(db: Database) -> list[str]:
    """App startup, before ``resume_pending`` (spec §10): a ``closing`` conversation whose
    close message is neither queued nor parked (a crash interrupted its turn) goes back to
    ``open``. Returns the ids it reopened."""
    repo, messages = ConversationRepo(db), MessageRepo(db)
    reopened: list[str] = []
    for conv in await repo.closing():
        pending = await db.fetchone(
            "SELECT 1 FROM messages WHERE conversation_id = ? AND role = 'user' "
            "AND status = 'queued' AND json_extract(metadata, '$.origin') = ? LIMIT 1",
            (conv.id, CLOSE_ORIGIN),
        )
        if pending is not None:
            continue  # resume_pending runs it (or its parked re-run)
        try:
            await repo.set_status(conv.id, "open", expect="closing")
        except ConversationStateConflict:
            continue
        await messages.insert(
            conv.project_id,
            conv.id,
            role="system",
            kind="notice",
            content=NOT_COMPLETED,
            metadata={"origin": CLOSE_ORIGIN},
        )
        reopened.append(conv.id)
    return reopened


def install_sessions(app: FastAPI) -> SessionService:
    """Lifespan: after install_plans and install_design_docs, before resume_pending (an
    engine keeps the hooks and services it was created with)."""
    from copperchat.agent.engine import ChainedHooks

    runtime = app.state.agent_runtime
    docs = getattr(app.state, "docs", None)
    runtime.docs = docs
    svc = SessionService(app.state.db, docs, runtime)
    previous = runtime.hooks_factory

    def hooks_factory(project: Project, engine: RunEngine) -> Any:
        base = previous(project, engine)
        chain = list(base.hooks) if isinstance(base, ChainedHooks) else [base]
        return ChainedHooks([*chain, SessionHooks(svc, project, engine)])

    runtime.hooks_factory = hooks_factory
    app.state.conversation_service = svc
    return svc
