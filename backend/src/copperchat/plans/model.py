"""Plan model (E-C11, spec §7): plans and their steps as immutable values."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from copperchat.errors import StructuredToolError

PlanStatus = Literal["proposed", "approved", "running", "paused", "done", "rejected", "aborted"]
StepStatus = Literal["pending", "running", "done", "failed", "interrupted"]
OPEN: frozenset[str] = frozenset({"proposed", "approved", "running", "paused"})
# No edits while one of these (owner decision): resume, revise or abort first.
EDIT_BLOCKING: frozenset[str] = frozenset({"proposed", "approved", "paused"})


class PlanStateError(StructuredToolError):
    """An event that is not allowed in the plan's current state (spec §7.2)."""

    http_status = 409

    def __init__(
        self,
        what: str,
        why: str,
        suggestion: str,
        *,
        code: str = "plan_state",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(what, why, suggestion, code=code, details=details)


class InvalidPlanArgument(StructuredToolError):
    """A plan argument out of range (unknown step, bad rollback target): HTTP 400."""

    http_status = 400

    def __init__(self, what: str, why: str, suggestion: str) -> None:
        super().__init__(what, why, suggestion, code="invalid_argument")


@dataclass(frozen=True)
class PlanStep:
    id: str
    plan_id: str
    idx: int  # 1-based
    title: str
    intent: str
    expected_changes: int
    touches: dict[str, list[str]]  # {refs, nets}
    verification: str
    status: StepStatus = "pending"
    run_id: str | None = None
    checkpoint_id: str | None = None
    change_count: int = 0
    summary: str | None = None
    failure: str | None = None
    erc_result_id: str | None = None


@dataclass(frozen=True)
class Plan:
    id: str
    project_id: str
    title: str
    summary: str
    status: PlanStatus
    revision: int
    created_run_id: str
    # The session that proposed the plan: it shows the card and receives Approve and
    # Resume (spec §5.4).
    conversation_id: str
    created_at: str
    updated_at: str
    steps: tuple[PlanStep, ...] = ()
    event_seq: int = 0
    pause_reason: str | None = None
    base_snapshot_id: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status in OPEN

    def step(self, idx: int) -> PlanStep:
        for s in self.steps:
            if s.idx == idx:
                return s
        raise InvalidPlanArgument(
            f"Plan step {idx} does not exist",
            f"The plan has steps 1-{len(self.steps)}",
            "Use a step number of the plan.",
        )

    @property
    def running_step(self) -> PlanStep | None:
        return next((s for s in self.steps if s.status == "running"), None)

    @property
    def next_step(self) -> PlanStep | None:
        """The first step that is not done (the one to execute next)."""
        return next((s for s in self.steps if s.status != "done"), None)

    @property
    def done_count(self) -> int:
        return sum(1 for s in self.steps if s.status == "done")


@dataclass(frozen=True)
class StepErc:
    """Step ERC summary for the DTO (from its `erc_results` row)."""

    errors: int
    warnings: int
    justifications: dict[str, str] = field(default_factory=dict)
    result_id: str | None = None


def to_dto(plan: Plan, erc: dict[str, StepErc] | None = None) -> dict[str, Any]:
    """PlanDTO (E-C11), camelCase on the wire; `erc` maps step id -> its ERC summary."""
    erc = erc or {}

    def step(s: PlanStep) -> dict[str, Any]:
        e = erc.get(s.id)
        return {
            "idx": s.idx,
            "title": s.title,
            "intent": s.intent,
            "expectedChanges": s.expected_changes,
            "touches": {
                "refs": list(s.touches.get("refs", [])),
                "nets": list(s.touches.get("nets", [])),
            },
            "verification": s.verification,
            "status": s.status,
            "changeCount": s.change_count,
            "checkpointId": s.checkpoint_id,
            "summary": s.summary,
            "failure": s.failure,
            "erc": None
            if e is None
            else {
                "errors": e.errors,
                "warnings": e.warnings,
                "justifications": dict(e.justifications),
                "resultId": e.result_id,
            },
        }

    return {
        "id": plan.id,
        "conversationId": plan.conversation_id,
        "projectId": plan.project_id,
        "title": plan.title,
        "summary": plan.summary,
        "status": plan.status,
        "revision": plan.revision,
        "pauseReason": plan.pause_reason,
        "steps": [step(s) for s in plan.steps],
        "createdAt": plan.created_at,
        "updatedAt": plan.updated_at,
    }
