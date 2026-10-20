"""Editing run hook (E-C5): turn bookkeeping and the turn-end ERC delta of an editing
turn (E8a, spec §5.1 / §8.3) and the plan pause at turn end (E8b, spec §7.2)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from copperchat.agent.engine import RunHooks, TurnInfo, TurnOutcome
from copperchat.changes import erc
from copperchat.editing.state import EditTurnState
from copperchat.kicad_tools import checks
from copperchat.projects import Project

if TYPE_CHECKING:
    from copperchat.agent.engine import RunEngine
    from copperchat.editing.service import EditingService

log = logging.getLogger(__name__)
_NOTE_LIMIT = 5  # new errors named in the agent note
_DESC_CHARS = 120  # the description quotes design text (labels, nets): data, capped


def _plural(n: int, word: str) -> str:
    return f"{n} new {word}{'' if n == 1 else 's'}"


def _brief(v: dict[str, Any]) -> str:
    desc = " ".join(str(v.get("description", "")).split())[:_DESC_CHARS]
    quoted = json.dumps(desc, ensure_ascii=False)  # data, not instructions; keep 10kΩ readable
    return f"{v.get('type')} (id {v.get('id')}, kicad-cli says: {quoted})"


class EditingHooks(RunHooks):
    def __init__(self, svc: EditingService, project: Project, engine: RunEngine) -> None:
        self.svc = svc
        self.project = project
        self.engine = engine

    async def on_turn_end(self, turn: TurnInfo, outcome: TurnOutcome) -> None:
        # Runs after HistoryHooks (ChainedHooks): the turn's changes are autosaved.
        state = self.svc.peek_turn(self.project.id, turn.run_id)
        try:
            if self._is_editing_turn(outcome, state):
                assert state is not None
                await self._erc_delta(turn, state)
        finally:
            try:
                if self.svc.plans is not None:
                    # E8b: a turn never leaves a plan running (spec §7.2).
                    await self.svc.plans.on_turn_end(
                        self.project, turn.run_id, outcome, self.engine
                    )
            except Exception:
                log.exception("pausing the plan of %s at turn end failed", self.project.rel_path)
            self.svc.drop_turn(self.project.id, turn.run_id)

    @staticmethod
    def _is_editing_turn(outcome: TurnOutcome, state: EditTurnState | None) -> bool:
        """A turn whose two-phase tools changed the schematic and whose in-memory
        design is trustworthy (no KiCad failure, nothing left inconsistent)."""
        return (
            not outcome.kicad_failed
            and not outcome.document_inconsistent
            and "sch" in outcome.changed_docs
            and state is not None
            and state.last_mutation_seq is not None
        )

    async def _erc_delta(self, turn: TurnInfo, state: EditTurnState) -> None:
        history = self.svc.history
        if history is None:
            return
        session = history.open_session(self.project)
        if session is None or session.read_only:
            return
        snap = await history.store(self.project).latest_for_run(turn.run_id)
        if snap is None:  # no undo point, no baseline (a mutation always takes one)
            return
        try:
            baseline = await erc.erc_baseline(history, self.project, snap)
            fresh = erc.erc_is_fresh(state)
            current = await erc.current_erc(session, state)
            if not fresh:
                # Run after HistoryHooks published this turn's view: the render
                # markers of the same state reuse it instead of a second ERC run.
                checks.seed_marker_cache(session, "sch", current)
            delta = erc.erc_delta(baseline, current)
            rid = await self.svc.erc_results.add(
                self.project.id, turn.run_id, state.active_step_id, snap.id, delta
            )
        except Exception as exc:  # a failed check must not fail the (finished) turn
            log.warning("turn-end ERC for %s failed: %s", self.project.rel_path, exc)
            await self.engine.post_notice(
                turn.conversation_id,
                "The ERC check after this turn could not run; run run_erc to check the design.",
                {"action": "erc-failed"},
            )
            return
        errors, warnings = len(delta.new_errors), len(delta.new_warnings)
        text = (
            f"ERC after this turn: {_plural(errors, 'error')}, "
            f"{_plural(warnings, 'warning')}, {len(delta.resolved)} resolved."
        )
        await self.engine.post_notice(
            turn.conversation_id,
            text,
            {"action": "erc-delta", "ercResultId": rid, "errors": errors, "warnings": warnings},
        )
        if errors:
            # Skill rule (§8.3): new errors are fixed or justified; tell the agent.
            named = "; ".join(_brief(v) for v in delta.new_errors[:_NOTE_LIMIT])
            more = errors - min(errors, _NOTE_LIMIT)
            self.engine.add_agent_note(
                f"The ERC after your last turn found {_plural(errors, 'error')}: {named}"
                + (f" and {more} more" if more else "")
                + ". Fix each one or justify it to the user.",
                conversation_id=turn.conversation_id,  # run-specific: only this session
            )
