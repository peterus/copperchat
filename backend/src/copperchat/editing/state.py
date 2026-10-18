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
