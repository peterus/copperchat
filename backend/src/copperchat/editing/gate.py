"""Edit gate (E-C5): what a two-phase tool must pass before it writes.

`BudgetGate` enforces the plan policy and the per-turn logical change budget (E-C6,
owner 2026-10-18); E8b's `PlanGate` extends it with plan states and steps. Refusals
happen in `before_prepare` / `check_budget`, i.e. before the turn snapshot (Review
Focus 4): nothing is written and no snapshot is taken.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from copperchat.errors import StructuredToolError

if TYPE_CHECKING:
    from copperchat.editing.service import EditingService
    from copperchat.kicad_tools.edit.changeset import ChangeSet
    from copperchat.tools.spec import ToolContext

MAX_PREEXISTING_SYMBOL_DELETES = 3


class EditGate(Protocol):
    async def before_prepare(self, ctx: ToolContext, tool: str) -> None: ...
    async def check_budget(self, ctx: ToolContext, cs: ChangeSet) -> None: ...
    async def record(self, ctx: ToolContext, cs: ChangeSet) -> None: ...
    async def budget_info(self, ctx: ToolContext) -> dict[str, Any] | None: ...


def _split_deletes(placed: set[str], cs: ChangeSet) -> tuple[int, int]:
    """(own placements deleted again, pre-existing symbols deleted) of one call."""
    deleted = set(cs.logical.deleted_symbols)
    own = len(deleted & placed)
    return own, len(deleted) - own


class BudgetGate:
    """Plan policy + turn budget, without plans (E2)."""

    def __init__(self, service: EditingService) -> None:
        self.service = service

    async def _policy(self, ctx: ToolContext) -> tuple[str, int]:
        settings = await self.service.settings_repo.get(ctx.project.id)
        return settings.plan_policy, settings.change_budget

    async def before_prepare(self, ctx: ToolContext, tool: str) -> None:
        policy, _ = await self._policy(ctx)
        state = self.service.turn(ctx.project.id, ctx.run_id)
        if policy == "always" and state.active_step_id is None:
            raise StructuredToolError(
                f"{tool} needs an approved plan step",
                "This project's plan policy is 'always': every change runs inside a plan step",
                "Propose the change with plan_propose and wait for the user's approval.",
                code="plan_step_required",
            )

    async def check_budget(self, ctx: ToolContext, cs: ChangeSet) -> None:
        policy, limit = await self._policy(ctx)
        if policy == "never":
            return
        state = self.service.turn(ctx.project.id, ctx.run_id)
        own, preexisting = _split_deletes(state.placed_this_turn, cs)
        cost = cs.logical.changes - own
        used_after = state.logical_used - own + cost
        if used_after > limit:
            raise StructuredToolError(
                f"{cs.tool} would exceed the change budget of this turn",
                f"It needs {cost} logical change(s); {state.logical_used} of {limit} are "
                "used in this turn without a plan",
                "Propose the remaining work with plan_propose (the user approves it), or "
                "ask the user to split the request.",
                code="plan_required",
                details={"needed": cost, "used": state.logical_used, "limit": limit},
            )
        if state.deleted_preexisting + preexisting > MAX_PREEXISTING_SYMBOL_DELETES:
            raise StructuredToolError(
                f"{cs.tool} would delete too many existing symbols in one turn",
                f"At most {MAX_PREEXISTING_SYMBOL_DELETES} symbols that existed before the "
                f"turn may be deleted without a plan; {state.deleted_preexisting} already "
                f"were, this call deletes {preexisting}",
                "Propose the deletions with plan_propose (the user approves them).",
                code="plan_required",
                details={
                    "deleted_preexisting": state.deleted_preexisting,
                    "this_call": preexisting,
                    "limit": MAX_PREEXISTING_SYMBOL_DELETES,
                },
            )

    async def record(self, ctx: ToolContext, cs: ChangeSet) -> None:
        """Book an applied change set (`cs.applied` is set) into the turn state."""
        state = self.service.turn(ctx.project.id, ctx.run_id)
        deleted = set(cs.logical.deleted_symbols)
        own, preexisting = _split_deletes(state.placed_this_turn, cs)
        # Deleting an own placement refunds it and costs nothing (E-C6).
        state.logical_used += cs.logical.changes - 2 * own
        state.deleted_preexisting += preexisting
        state.placed_this_turn -= deleted
        state.placed_this_turn |= set(cs.logical.placed_symbols)
        state.last_mutation_seq = state.call_seq

    async def budget_info(self, ctx: ToolContext) -> dict[str, Any] | None:
        policy, limit = await self._policy(ctx)
        if policy == "never":
            return None
        state = self.service.turn(ctx.project.id, ctx.run_id)
        return {"used": state.logical_used, "limit": limit}
