"""Per-turn editing state (E-C5): logical change budget, own placements, call order."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EditTurnState:
    """One per (project_id, run_id); dropped at turn end by EditingHooks."""

    logical_used: int = 0
    deleted_preexisting: int = 0  # non-power symbols that existed before the turn
    placed_this_turn: set[str] = field(default_factory=set)  # symbol UUIDs
    call_seq: int = 0  # every tool call of the turn
    last_mutation_seq: int | None = None
    last_erc_seq: int | None = None  # E8a: no double ERC runs
    last_erc: list[dict[str, Any]] | None = None
    last_render_seq: int | None = None  # render_view(doc="sch")
    active_step_id: str | None = None  # E8b
    # E8b: the active plan step of this turn (PlanGate counts its calls against the
    # step's expected changes instead of the turn budget).
    step_idx: int | None = None
    step_expected: int = 0
    step_used: int = 0
    step_begin_seq: int | None = None  # call_seq of plan_step_begin (render reminder)
    step_placed: set[str] = field(default_factory=set)  # symbols placed inside the step
    # (step id, violation ids) of the last `erc_errors_open` refusal of plan_step_done.
    erc_refusal: tuple[str, frozenset[str]] | None = None
    # A failed edit could not be undone exactly: every further edit of the turn is refused
    # (the turn ends and the session is reloaded).
    halted: bool = False
