"""PlanGate (E-C5/E-C6, spec §7.1): BudgetGate plus plan discipline.

- a plan `proposed` / `approved` / `paused` -> every edit refused (`plan_open`);
- a plan `running` -> edits only inside this turn's active step (`plan_step_required`),
  counted against the step's expected changes + 50 % instead of the turn budget;
  exceeding it pauses the plan (step `failed`) and refuses the call
  (`plan_step_exceeded`);
- no open plan -> BudgetGate (policy + turn budget; `always` needs a step).

Every refusal happens before the turn snapshot and before any write.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from copperchat.editing.gate import BudgetGate
from copperchat.errors import StructuredToolError
from copperchat.plans.model import EDIT_BLOCKING
from copperchat.plans.service import step_limit

if TYPE_CHECKING:
    from copperchat.editing.service import EditingService
    from copperchat.editing.state import EditTurnState
    from copperchat.kicad_tools.edit.changeset import ChangeSet
    from copperchat.plans.service import PlanService
    from copperchat.tools.spec import ToolContext


# What the agent can do about an open plan that blocks an edit, per status.
_BLOCKED_HINT = {
    "proposed": "Wait for the user to approve or reject the plan, or revise it with plan_revise.",
    "approved": "The plan is approved: call plan_step_begin for its next step and make "
    "the change inside it (or ask the user to abort the plan).",
    "paused": "Wait for the user to resume or roll back the plan, revise it with "
    "plan_revise, or ask the user to abort it.",
}


def _foreign_hint(owner: str) -> str:
    return (
        f"The open plan belongs to {owner}: make the change there, or ask the user to "
        "finish or abort that plan first."
    )


class PlanGate(BudgetGate):
    def __init__(self, service: EditingService, plans: PlanService) -> None:
        super().__init__(service)
        self.plans = plans

    async def before_prepare(self, ctx: ToolContext, tool: str) -> None:
        open_ = await self.plans.repo.open_status(ctx.project.id)
        state = self.service.turn(ctx.project.id, ctx.run_id)
        if state.active_step_id is not None and (open_ is None or open_[1] != "running"):
            # The step's plan is no longer running (paused/closed meanwhile): the step
            # state is stale and must never exempt a call from the turn budget.
            self.plans.clear_step(state)
        if open_ is None:
            await super().before_prepare(ctx, tool)
            return
        plan_id, status = open_
        # The plan of another session blocks this one too (spec §5.4): say whose.
        blocked = status in EDIT_BLOCKING or state.active_step_id is None
        owner = await self.plans.foreign_owner(ctx) if blocked else None
        if status in EDIT_BLOCKING:
            hint = _BLOCKED_HINT.get(status, "Ask the user to finish or abort the plan.")
            raise StructuredToolError(
                f"{tool} refused: a plan is {status}" + (f" in {owner}" if owner else ""),
                "No edits happen outside the steps of an open plan",
                _foreign_hint(owner) if owner else hint,
                code="plan_open",
                details={"plan_id": plan_id, "status": status, "owner": owner},
            )
        if state.active_step_id is None:
            raise StructuredToolError(
                f"{tool} refused: "
                + (f"the plan of {owner} is running" if owner else "no plan step is active"),
                "While a plan runs, every change belongs to one of its steps",
                _foreign_hint(owner) if owner else "Call plan_step_begin for the next step first.",
                code="plan_step_required",
                details={"plan_id": plan_id, "owner": owner},
            )

    async def check_budget(self, ctx: ToolContext, cs: ChangeSet) -> None:
        state = self.service.turn(ctx.project.id, ctx.run_id)
        if state.active_step_id is None:
            await super().check_budget(ctx, cs)
            return
        used_after = state.step_used + self._step_cost(state, cs)
        limit = step_limit(state.step_expected)
        if used_after <= limit:
            return
        idx, used = state.step_idx, state.step_used
        failure = (
            f"needed {used_after} logical changes; step {idx} expected "
            f"{state.step_expected} (limit {limit} with 50 % tolerance)"
        )
        await self.plans.fail_step(ctx, "step_exceeded", failure)
        raise StructuredToolError(
            f"{cs.tool} would exceed plan step {idx}'s expected changes",
            f"The step {failure}; the plan is paused and the step marked failed",
            "Do not improvise: revise the plan with plan_revise (the user approves it "
            "again) and explain why the step needs more changes.",
            code="plan_step_exceeded",
            details={"used": used, "needed": used_after, "limit": limit},
        )

    async def record(self, ctx: ToolContext, cs: ChangeSet) -> None:
        state = self.service.turn(ctx.project.id, ctx.run_id)
        step_id = state.active_step_id
        if step_id is None:
            await super().record(ctx, cs)
            return
        # Inside a step: own placements and refunds are booked as usual, but the
        # logical changes go to the step, not the turn budget (and the <= 3 deletions
        # rule is the turn budget's: the user approved the plan's deletions).
        cost = self._step_cost(state, cs)
        used, deleted = state.logical_used, state.deleted_preexisting
        placed = set(state.placed_this_turn)
        await super().record(ctx, cs)
        # Step placements stay out of the turn's own-placement ledger: deleting them
        # after the plan finished must not refund turn budget that was never charged.
        state.logical_used, state.deleted_preexisting = used, deleted
        state.placed_this_turn = placed - set(cs.logical.deleted_symbols)
        state.step_used = max(0, state.step_used + cost)
        state.step_placed -= set(cs.logical.deleted_symbols)
        state.step_placed |= set(cs.logical.placed_symbols)
        # Persisted with the step: a revision or resume continues in another run
        # (spec §9.2), possibly after a server restart.
        async with self.plans.lock(ctx.project.id):
            await self.plans.repo.save_step_counters(step_id, state.step_used, state.step_placed)

    @staticmethod
    def _step_cost(state: EditTurnState, cs: ChangeSet) -> int:
        """Logical changes of a call inside a step: deleting a symbol placed in this very
        step refunds its placement and costs nothing (E-C6); every other deletion costs
        as usual."""
        own = len(set(cs.logical.deleted_symbols) & state.step_placed)
        return cs.logical.changes - 2 * own

    async def budget_info(self, ctx: ToolContext) -> dict[str, Any] | None:
        state = self.service.turn(ctx.project.id, ctx.run_id)
        if state.active_step_id is None:
            return await super().budget_info(ctx)
        return {
            "used": state.step_used,
            "limit": step_limit(state.step_expected),
            "scope": "step",
            "step": state.step_idx,
        }
