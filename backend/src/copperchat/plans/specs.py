"""Plan tools (spec §6.3): plan_propose, plan_revise, plan_step_begin, plan_step_done."""

from __future__ import annotations

from typing import Any

from copperchat.errors import StructuredToolError
from copperchat.plans.service import (
    EXPECTED_MAX,
    MAX_STEPS,
    REASON_MAX,
    SUMMARY_MAX,
    TEXT_MAX,
    TITLE_MAX,
    TOUCH_ITEM_MAX,
    TOUCH_MAX,
    PlanService,
    step_limit,
)
from copperchat.tools.spec import ToolContext, ToolResult, ToolSpec

_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": TITLE_MAX},
        "intent": {"type": "string", "maxLength": TEXT_MAX},
        "expected_changes": {
            "type": "integer",
            "minimum": 1,
            "maximum": EXPECTED_MAX,
            "description": "Logical changes of the step (a placed part, a connection, a "
            "label/power symbol per pin, a field edit, a move, a delete each count 1).",
        },
        "touches": {
            "type": "object",
            "properties": {
                "refs": {
                    "type": "array",
                    "maxItems": TOUCH_MAX,
                    "items": {"type": "string", "maxLength": TOUCH_ITEM_MAX},
                },
                "nets": {
                    "type": "array",
                    "maxItems": TOUCH_MAX,
                    "items": {"type": "string", "maxLength": TOUCH_ITEM_MAX},
                },
            },
            "additionalProperties": False,
        },
        "verification": {"type": "string", "maxLength": TEXT_MAX},
    },
    "required": ["title", "intent", "expected_changes", "verification"],
    "additionalProperties": False,
}
_STEPS: dict[str, Any] = {
    "type": "array",
    "items": _STEP_SCHEMA,
    "minItems": 1,
    "maxItems": MAX_STEPS,
}


def _plans(ctx: ToolContext) -> PlanService:
    plans = ctx.editing.plans if ctx.editing is not None else None
    if plans is None or ctx.run_id is None:
        raise StructuredToolError(
            "Plans are not available",
            "There is no agent turn with the editing service",
            "Call plan tools during an agent turn.",
        )
    return plans


async def _propose(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    plan = await _plans(ctx).propose(ctx, args.get("title"), args.get("summary"), args.get("steps"))
    return ToolResult(
        data={
            "plan_id": plan.id,
            "status": plan.status,
            "revision": plan.revision,
            "steps": len(plan.steps),
            "next": "The user approves or rejects the plan in its card. Do not change the "
            "design until they approve it; end your turn now and wait.",
        }
    )


async def _revise(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    plan = await _plans(ctx).revise(ctx, args.get("steps"), args.get("reason"))
    nxt = plan.next_step  # a step paused mid-way by this revision continues first
    return ToolResult(
        data={
            "plan_id": plan.id,
            "status": plan.status,
            "revision": plan.revision,
            "steps": len(plan.steps),
            "kept_done_steps": plan.done_count,
            **({"paused_step": nxt.idx} if nxt is not None and nxt.status == "paused" else {}),
            "next": "The user must approve the revised plan; end your turn now and wait.",
        }
    )


async def _step_begin(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    step = await _plans(ctx).step_begin(ctx, args.get("step"))
    return ToolResult(
        data={
            "step": step.idx,
            "status": step.status,
            "expected_changes": step.expected_changes,
            "change_limit": step_limit(step.expected_changes),
            "next": "Make the step's changes, render the region (render_view doc='sch'), "
            "then call plan_step_done.",
        }
    )


async def _step_done(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    data = await _plans(ctx).step_done(
        ctx, args.get("step"), args.get("summary"), args.get("erc_justifications")
    )
    return ToolResult(data=data)


TOOL_SPECS = [
    ToolSpec(
        "plan_propose",
        "Propose a plan for a larger schematic change (more than the per-turn change "
        "budget, or whenever the project's plan policy asks for one). Creates the plan as "
        "'proposed' and shows it to the user as a card; nothing is changed until the user "
        "approves it. Each step: title, intent, expected_changes (logical changes), "
        "touches {refs, nets}, verification (what you will check). Refused while another "
        "plan is open (use plan_revise).",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "maxLength": TITLE_MAX},
                "summary": {"type": "string", "maxLength": SUMMARY_MAX},
                "steps": _STEPS,
            },
            "required": ["title", "summary", "steps"],
            "additionalProperties": False,
        },
        _propose,
    ),
    ToolSpec(
        "plan_revise",
        "Revise the open plan while it is proposed, running or paused (the user approves "
        "it again). The finished steps are kept; `steps` replaces the remaining ones "
        "(numbered after the finished steps). Mid-step: if the running step needs more "
        "changes than expected, call plan_revise before exceeding its limit and say why "
        "in reason; the step pauses with its edits and used count and continues as the "
        "first of the new steps once the user approves (begin it again then). Exceeding "
        "the limit still fails the step. Budget: free.",
        {
            "type": "object",
            "properties": {
                "steps": _STEPS,
                "reason": {"type": "string", "maxLength": REASON_MAX},
            },
            "required": ["steps", "reason"],
            "additionalProperties": False,
        },
        _revise,
    ),
    ToolSpec(
        "plan_step_begin",
        "Begin the next step of the approved plan (steps run in order). Edits are only "
        "allowed inside a begun step and count against its expected_changes (+50 %).",
        {
            "type": "object",
            "properties": {"step": {"type": "integer", "minimum": 1}},
            "required": ["step"],
            "additionalProperties": False,
        },
        _step_begin,
    ),
    ToolSpec(
        "plan_step_done",
        "Finish the active plan step: runs the step's ERC delta and takes a checkpoint. "
        "Refused (erc_errors_open, with the list) while a new ERC error is neither fixed "
        "nor justified in erc_justifications {violation_id: reason}; leaving the same "
        "errors open twice pauses the plan. Render the step's region before calling it.",
        {
            "type": "object",
            "properties": {
                "step": {"type": "integer", "minimum": 1},
                "summary": {"type": "string", "maxLength": SUMMARY_MAX},
                "erc_justifications": {
                    "type": "object",
                    "additionalProperties": {"type": "string", "maxLength": REASON_MAX},
                },
            },
            "required": ["step", "summary"],
            "additionalProperties": False,
        },
        _step_done,
    ),
]
