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

from copperchat.editing.gate import BudgetGate, _split_deletes
from copperchat.errors import StructuredToolError
from copperchat.plans.model import EDIT_BLOCKING
from copperchat.plans.service import step_limit

if TYPE_CHECKING:
    from copperchat.editing.service import EditingService
    from copperchat.kicad_tools.edit.changeset import ChangeSet
    from copperchat.plans.service import PlanService
    from copperchat.tools.spec import ToolContext


class PlanGate(BudgetGate):
    def __init__(self, service: EditingService, plans: PlanService) -> None:
        super().__init__(service)
        self.plans = plans

    async def before_prepare(self, ctx: ToolContext, tool: str) -> None:
        open_ = await self.plans.repo.open_status(ctx.project.id)
        if open_ is None:
            await super().before_prepare(ctx, tool)
            return
        plan_id, status = open_
        if status in EDIT_BLOCKING:
            raise StructuredToolError(
                f"{tool} refused: a plan is {status}",
                "No edits happen outside the steps of an open plan",
                "Wait for the user to approve or resume the plan, revise it with "
                "plan_revise, or ask the user to abort it.",
                code="plan_open",
                details={"plan_id": plan_id, "status": status},
            )
        state = self.service.turn(ctx.project.id, ctx.run_id)
        if state.active_step_id is None:
            raise StructuredToolError(
                f"{tool} refused: no plan step is active",
                "While a plan runs, every change belongs to one of its steps",
                "Call plan_step_begin for the next step first.",
                code="plan_step_required",
                details={"plan_id": plan_id},
            )

    async def check_budget(self, ctx: ToolContext, cs: ChangeSet) -> None:
        state = self.service.turn(ctx.project.id, ctx.run_id)
        if state.active_step_id is None:
            await super().check_budget(ctx, cs)
            return
        own, _ = _split_deletes(state.placed_this_turn, cs)
        used_after = state.step_used + cs.logical.changes - 2 * own
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
        if state.active_step_id is None:
            await super().record(ctx, cs)
            return
        # Inside a step: own placements and refunds are booked as usual, but the
        # logical changes go to the step, not the turn budget (and the <= 3 deletions
        # rule is the turn budget's: the user approved the plan's deletions).
        used, deleted = state.logical_used, state.deleted_preexisting
        await super().record(ctx, cs)
        state.step_used += state.logical_used - used
        state.logical_used, state.deleted_preexisting = used, deleted

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
