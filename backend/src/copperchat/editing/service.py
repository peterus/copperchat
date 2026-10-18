"""EditingService (E-C5): per-turn state, the edit gate and the change log."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from copperchat.changes.repo import ChangeEntry, ChangeRepo
from copperchat.db import Database
from copperchat.db.repos.editing_settings import EditingSettingsRepo
from copperchat.editing.gate import BudgetGate, EditGate
from copperchat.editing.state import EditTurnState

if TYPE_CHECKING:
    from copperchat.agent.broadcaster import Broadcaster
    from copperchat.tools.spec import ToolContext

log = logging.getLogger(__name__)
# (project_id, run_id, plan_step_id, count) after change rows were written (E8a: WS frame).
ChangesRecorded = Callable[[str, str, str | None, int], Awaitable[None]]


class EditingService:
    def __init__(
        self,
        *,
        db: Database,
        broadcaster: Broadcaster | None,
        settings_repo: EditingSettingsRepo,
        gate: EditGate | None = None,
    ) -> None:
        self.db = db
        self.broadcaster = broadcaster
        self.settings_repo = settings_repo
        self.gate: EditGate = gate if gate is not None else BudgetGate(self)
        self.changes = ChangeRepo(db)
        self.on_changes_recorded: list[ChangesRecorded] = []
        self._turns: dict[tuple[str, str | None], EditTurnState] = {}

    def turn(self, project_id: str, run_id: str | None) -> EditTurnState:
        key = (project_id, run_id)
        state = self._turns.get(key)
        if state is None:
            state = self._turns[key] = EditTurnState()
        return state

    def drop_turn(self, project_id: str, run_id: str | None) -> None:
        self._turns.pop((project_id, run_id), None)

    def note_call(self, ctx: ToolContext, tool: str, args: dict[str, Any] | None = None) -> None:
        """Every tool call (any spec) counts in the turn's call order."""
        state = self.turn(ctx.project.id, ctx.run_id)
        state.call_seq += 1
        if tool == "render_view" and (args or {}).get("doc") == "sch":
            state.last_render_seq = state.call_seq

    async def record_changes(self, ctx: ToolContext, entries: list[ChangeEntry]) -> int:
        """Write change rows of one applied call, then notify listeners (never fails the
        call: the edit itself is done and verified)."""
        if not entries or ctx.run_id is None:
            return 0
        count = await self.changes.add_many(entries)
        step = entries[0].plan_step_id
        for cb in list(self.on_changes_recorded):
            try:
                await cb(ctx.project.id, ctx.run_id, step, count)
            except Exception:
                log.exception("on_changes_recorded callback failed")
        return count
