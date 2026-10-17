"""Run hooks: external-change check before a turn, snapshot before the first mutation,
autosave + view refresh after a turn that changed documents, crash recovery."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from copperchat.agent.engine import RunHooks, TurnInfo, TurnOutcome
from copperchat.history.store import compute_hashes_async, ensure_gitignore
from copperchat.kicad_session import DocKind, KicadUnavailable
from copperchat.projects import Project

if TYPE_CHECKING:
    from copperchat.agent.engine import RunEngine
    from copperchat.history.service import HistoryService

log = logging.getLogger(__name__)
LABEL_CHARS = 80
_RECOVER_ATTEMPTS = 2  # the first call after a crash restarts the server and still raises


class HistoryHooks(RunHooks):
    def __init__(self, svc: HistoryService, project: Project, engine: RunEngine) -> None:
        self.svc = svc
        self.project = project
        self.engine = engine

    async def _record_hashes(self) -> None:
        await self.svc.hashes.replace(self.project.id, await compute_hashes_async(self.project))

    async def on_engine_start(self) -> None:
        ensure_gitignore(self.project)
        if not await self.svc.hashes.get(self.project.id):
            await self._record_hashes()

    async def on_turn_start(self, turn: TurnInfo) -> None:
        """Hash check (Review Focus 4). The engine awaits this before releasing the
        message, so a reload and the agent note reach this very turn."""
        async with self.svc.lock(self.project):
            stored = await self.svc.hashes.get(self.project.id)
            now = await compute_hashes_async(self.project)
            if not stored:
                await self.svc.hashes.replace(self.project.id, now)
                return
            changed = sorted(k for k in stored.keys() | now.keys() if stored.get(k) != now.get(k))
            if not changed:
                return
            reloaded = True
            if self.svc.open_session(self.project) is not None:
                try:
                    session = await self.svc.sessions.reopen(self.project)
                    await self.svc.publish_view(
                        self.project, session, self.svc.docs_of(self.project)
                    )
                except KicadUnavailable as exc:
                    # The turn's tools then fail and the engine reports KiCad (X18).
                    log.warning("reload after external change failed: %s", exc)
                    reloaded = False
            await self.svc.hashes.replace(self.project.id, now)
        files = ", ".join(changed)
        await self.engine.post_notice(
            f"Files changed outside CopperChat since the last turn ({files}). "
            + (
                "CopperChat reloaded them; the agent works on the new state."
                if reloaded
                else "Reloading them in KiCad failed."
            ),
            {"action": "external-change", "files": changed},
        )
        self.engine.add_agent_note(
            f"The design files {files} were changed outside CopperChat"
            + (" and reloaded. " if reloaded else "; KiCad could not reload them. ")
            + "Re-read any design data you rely on."
        )

    async def on_first_mutation(self, turn: TurnInfo) -> None:
        """Undo point before the turn's first mutating tool call. An exception here
        makes that tool call fail, so nothing is changed without a snapshot."""
        session = self.svc.open_session(self.project)
        if session is None or session.read_only:
            return  # a read-only project is never changed (Review Focus 3)
        docs = self.svc.docs_of(self.project)
        preview = await self.svc.render_preview(session, "pcb" if "pcb" in docs else "sch")
        label = " ".join(turn.text.split())[:LABEL_CHARS]
        await self.svc.store(self.project).snapshot(turn.run_id, label, preview)

    async def on_turn_end(self, turn: TurnInfo, outcome: TurnOutcome) -> None:
        if outcome.kicad_failed:
            # The engine already posted the "KiCad stopped responding" row for this
            # turn (X18). Its in-memory changes are lost: no autosave.
            await self._recover(turn)
            return
        # Every reason incl. interrupted / rate_limited: what changed is in KiCad's memory.
        docs: set[DocKind] = set(outcome.changed_docs)
        if not docs:
            return
        session = self.svc.open_session(self.project)
        if session is None or session.read_only:
            return
        async with self.svc.lock(self.project):
            try:
                for doc in sorted(docs):
                    await session.save(doc)
                # save() rewrites files even without edits: hash only after our own save.
                await self._record_hashes()
                await self.svc.publish_view(self.project, session, docs)
                return
            except Exception as exc:
                log.warning("autosave failed for %s: %s", self.project.rel_path, exc)
                failure = exc
        # Not a kicad_failed turn: the engine has posted nothing, so report the save.
        await self.engine.post_error(
            f"Autosave failed ({failure}). The design on disk is at its last saved state.",
            {"code": "autosave_failed"},
        )
        await self._recover(turn)

    async def _recover(self, turn: TurnInfo) -> None:
        """Make sure KiCad runs again and offer the turn's snapshot. Never posts the
        "KiCad stopped responding" row itself - the engine owns that (index X18)."""
        session = self.svc.open_session(self.project)
        if session is not None:
            error: KicadUnavailable | None = None
            for _ in range(_RECOVER_ATTEMPTS):
                try:
                    await session.call(lambda h: h.kicad.ping())
                    error = None
                    break
                except KicadUnavailable as exc:
                    error = exc
            if error is not None:
                await self.engine.post_error(
                    f"KiCad could not be restarted: {error}. Reopen the project to retry.",
                    {"code": "kicad_restart_failed"},
                )
        snap = await self.svc.store(self.project).latest_for_run(turn.run_id)
        if snap is not None:
            await self.engine.post_notice(
                "A snapshot from before this turn exists. Restore it to undo this turn's changes.",
                {"action": "restore", "snapshotId": snap.id},
            )
