"""Plan state machine (spec §7.2), pure: `transition(plan, event, **kw) -> Plan`.

| From                          | Event            | To        |
|-------------------------------|------------------|-----------|
| —                             | propose          | proposed  (revision 1, `new_plan`) |
| proposed                      | reject           | rejected, or paused at the previous revision |
| proposed, running, paused     | revise           | proposed  (revision + 1; running step paused) |
| proposed                      | approve          | approved  (event_seq + 1) |
| approved, running             | step_begin       | running   (only the next step, none running) |
| running                       | step_done        | running / done after the last step |
| running                       | interrupt        | paused    (step interrupted) |
| running                       | turn_end         | paused    (turn_ended, step interrupted) |
| running                       | fail             | paused    (step failed with a reason) |
| approved, running, paused     | external_change  | paused    (external_change, step failed) |
| paused                        | resume           | approved  (event_seq + 1) |
| paused                        | rollback         | paused    (later steps pending) |
| proposed, approved, paused    | abort            | aborted   |

Any other combination raises `PlanStateError(code="plan_state")`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from copperchat.plans.model import Plan, PlanStateError, PlanStatus, PlanStep, StepStatus

# Begun steps that continue with their counters (step_begin, revise).
RESUMABLE: frozenset[str] = frozenset({"paused", "interrupted"})
_ALLOWED: dict[str, frozenset[str]] = {
    "reject": frozenset({"proposed"}),
    "revise": frozenset({"proposed", "running", "paused"}),
    "approve": frozenset({"proposed"}),
    "step_begin": frozenset({"approved", "running"}),
    "step_done": frozenset({"running"}),
    "interrupt": frozenset({"running"}),
    "turn_end": frozenset({"running"}),
    "fail": frozenset({"running"}),
    "external_change": frozenset({"approved", "running", "paused"}),
    "resume": frozenset({"paused"}),
    "rollback": frozenset({"paused"}),
    "abort": frozenset({"proposed", "approved", "paused"}),
}
_HINT = {
    "reject": "Only a proposed plan can be rejected.",
    "revise": "Revise the plan only while it is proposed, running or paused; an approved "
    "plan: begin its next step first.",
    "approve": "Only a proposed plan can be approved.",
    "step_begin": "Steps begin only in an approved or running plan; wait for the user's "
    "approval (or Resume).",
    "step_done": "Finish a step only while the plan is running.",
    "resume": "Only a paused plan can be resumed.",
    "rollback": "Only a paused plan can be rolled back; stop the agent first.",
    "abort": "A running plan cannot be aborted; stop the agent first.",
}


def build_steps(
    plan_id: str, specs: Iterable[dict[str, Any]], start: int = 1
) -> tuple[PlanStep, ...]:
    """PlanSteps from validated step specs (`title, intent, expected_changes, touches,
    verification`), numbered from `start`, fresh ids."""
    return tuple(
        PlanStep(
            id=uuid.uuid4().hex,
            plan_id=plan_id,
            idx=start + i,
            title=s["title"],
            intent=s["intent"],
            expected_changes=int(s["expected_changes"]),
            touches={
                "refs": list(s.get("touches", {}).get("refs", [])),
                "nets": list(s.get("touches", {}).get("nets", [])),
            },
            verification=s["verification"],
        )
        for i, s in enumerate(specs)
    )


def new_plan(
    *,
    plan_id: str,
    project_id: str,
    title: str,
    summary: str,
    steps: tuple[PlanStep, ...],
    run_id: str,
    conversation_id: str,
    now: str,
) -> Plan:
    """`plan_propose`: a proposed plan at revision 1, owned by `conversation_id`."""
    return Plan(
        id=plan_id,
        project_id=project_id,
        title=title,
        summary=summary,
        status="proposed",
        revision=1,
        created_run_id=run_id,
        conversation_id=conversation_id,
        created_at=now,
        updated_at=now,
        steps=steps,
    )


def _refuse(plan: Plan, event: str) -> PlanStateError:
    return PlanStateError(
        f"Plan event '{event}' is not allowed",
        f"The plan is {plan.status}",
        _HINT.get(event, "Check the plan's state with the plan card."),
    )


def _set_step(plan: Plan, idx: int, **changes: Any) -> tuple[PlanStep, ...]:
    return tuple(replace(s, **changes) if s.idx == idx else s for s in plan.steps)


def with_step(plan: Plan, idx: int, **changes: Any) -> Plan:
    """The plan with step `idx` changed (bookkeeping fields, not a state transition)."""
    return replace(plan, steps=_set_step(plan, idx, **changes))


def _pause(plan: Plan, reason: str, step_status: StepStatus, failure: str | None, now: str) -> Plan:
    steps = plan.steps
    running = plan.running_step
    if running is not None:
        steps = _set_step(plan, running.idx, status=step_status, failure=failure)
    return replace(plan, status="paused", pause_reason=reason, steps=steps, updated_at=now)


def _revise(plan: Plan, new_steps: tuple[PlanStep, ...], reason: str | None, now: str) -> Plan:
    """Finished steps stay (their checkpoints are the plan's history); `new_steps`
    replaces everything after them. A begun step (running, or paused / interrupted)
    becomes the first new step, `paused`, keeping its id, run and counters: its edits so
    far belong to it (spec §9.2). Revising a running or paused plan keeps that state as
    `previous` for a reject; revising a proposed one keeps its `previous`."""
    base = plan
    if plan.status == "running":
        base = _pause(plan, "revised", "paused", None, now)
    previous = replace(base, previous=None) if plan.status != "proposed" else plan.previous
    kept = tuple(s for s in base.steps if s.status == "done")
    begun = next((s for s in base.steps if s.status != "done"), None)
    if begun is not None and begun.status in RESUMABLE and new_steps:
        first = replace(
            new_steps[0],
            id=begun.id,
            status="paused",
            run_id=begun.run_id,
            used=begun.used,
            placed=begun.placed,
        )
        new_steps = (first, *new_steps[1:])
    return replace(
        plan,
        status="proposed",
        revision=plan.revision + 1,
        pause_reason=None,
        revise_reason=reason,
        previous=previous,
        steps=kept + new_steps,
        updated_at=now,
    )


def transition(plan: Plan, event: str, *, now: str, **kw: Any) -> Plan:
    allowed = _ALLOWED.get(event)
    if allowed is None:
        raise ValueError(f"unknown plan event {event!r}")
    if plan.status not in allowed:
        raise _refuse(plan, event)
    status: PlanStatus
    if event == "reject" and plan.previous is not None:
        # A revision of a plan with edits on disk: back to the revision it replaced,
        # paused where it stopped (spec §9.2); nothing is discarded.
        return replace(
            plan.previous,
            status="paused",
            pause_reason="revision_rejected",
            event_seq=plan.event_seq,
            updated_at=now,
            previous=None,
        )
    if event in ("reject", "abort"):
        status = "rejected" if event == "reject" else "aborted"
        return replace(plan, status=status, updated_at=now)
    if event == "revise":
        return _revise(plan, kw["steps"], kw.get("reason"), now)
    if event in ("approve", "resume"):
        return replace(
            plan,
            status="approved",
            event_seq=plan.event_seq + 1,
            pause_reason=None,
            updated_at=now,
            previous=None,
        )
    if event == "step_begin":
        idx: int = kw["idx"]
        step = plan.step(idx)
        running = plan.running_step
        if running is not None:
            raise PlanStateError(
                f"Plan step {idx} cannot begin",
                f"Step {running.idx} is still running",
                f"Finish step {running.idx} with plan_step_done first.",
            )
        nxt = plan.next_step
        if nxt is None or nxt.idx != idx:
            raise PlanStateError(
                f"Plan step {idx} cannot begin",
                f"Steps run in order; step {step.idx} is {step.status}"
                + (f" and the next step is {nxt.idx}" if nxt is not None else ""),
                f"Begin step {nxt.idx} instead." if nxt is not None else "The plan is complete.",
            )
        # A paused or interrupted step continues with its counters (its edits are on
        # disk); any other step starts its budget afresh.
        counters = {} if step.status in RESUMABLE else {"used": 0, "placed": ()}
        steps = _set_step(
            plan, idx, status="running", run_id=kw["run_id"], failure=None, **counters
        )
        return replace(plan, status="running", pause_reason=None, steps=steps, updated_at=now)
    if event == "step_done":
        idx = kw["idx"]
        step = plan.step(idx)
        if step.status != "running":
            raise PlanStateError(
                f"Plan step {idx} cannot be finished",
                f"Step {idx} is {step.status}, not running",
                "Call plan_step_begin for the step first.",
            )
        steps = _set_step(
            plan,
            idx,
            status="done",
            checkpoint_id=kw.get("checkpoint_id"),
            change_count=int(kw.get("change_count", 0)),
            summary=kw.get("summary"),
            erc_result_id=kw.get("erc_result_id"),
            failure=None,
        )
        done = replace(plan, steps=steps, updated_at=now)
        if done.next_step is None:
            return replace(done, status="done")
        return done
    if event == "interrupt":
        return _pause(plan, kw.get("reason", "interrupted"), "interrupted", None, now)
    if event == "turn_end":
        return _pause(plan, "turn_ended", "interrupted", None, now)
    if event == "fail":
        return _pause(plan, kw.get("reason", "step_failed"), "failed", kw["failure"], now)
    if event == "external_change":
        return _pause(
            plan, "external_change", "failed", "the design files changed outside CopperChat", now
        )
    # rollback
    to_step: int = kw["to_step"]
    if to_step != 0:
        target = plan.step(to_step)
        if target.status != "done" or target.checkpoint_id is None:
            raise PlanStateError(
                f"Cannot roll back to step {to_step}",
                f"Step {to_step} is {target.status} and has no checkpoint",
                "Roll back to a finished step or to the start (0).",
            )
    steps = tuple(
        replace(
            s,
            status="pending",
            run_id=None,
            checkpoint_id=None,
            change_count=0,
            summary=None,
            failure=None,
            erc_result_id=None,
            used=0,
            placed=(),
            change_base=0,
        )
        if s.idx > to_step
        else s
        for s in plan.steps
    )
    return replace(plan, steps=steps, pause_reason="rolled_back", updated_at=now)
