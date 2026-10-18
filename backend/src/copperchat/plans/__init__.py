"""Plans (E-C11, spec §7): model, state machine, gate, service, tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

    from copperchat.plans.service import PlanService

__all__ = ["install_plans"]


def install_plans(app: FastAPI) -> PlanService:
    """Lifespan: after install_editing, before resume_pending. Replaces the editing
    gate with PlanGate, lets EditingHooks pause running plans at turn end and pauses a
    running plan when the history hook sees an external change."""
    from copperchat.plans.gate import PlanGate
    from copperchat.plans.service import PlanService

    editing = app.state.editing
    runtime = app.state.agent_runtime
    history = getattr(app.state, "history", None)
    svc = PlanService(
        db=app.state.db,
        editing=editing,
        history=history,
        runtime=runtime,
        broadcaster=runtime.broadcaster,
    )
    editing.gate = PlanGate(editing, svc)
    editing.plans = svc
    if history is not None:
        history.on_external_change.append(svc.on_external_change)
    app.state.plans = svc
    return svc
