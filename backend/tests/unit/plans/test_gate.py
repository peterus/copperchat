"""PlanGate (E8b Task 2): open plans, steps, step tolerance, turn budget, policy."""

from __future__ import annotations

import pytest

from copperchat.plans import machine
from tests.unit.plans.conftest import NOW, PID, Harness, make_plan


@pytest.mark.parametrize("status", ["proposed", "approved", "paused"])
async def test_plan_open_refuses_mutation(h: Harness, status: str) -> None:
    plan = await h.store(make_plan())
    await h.set_status(plan, status)
    out = await h.call(h.spec())
    assert out["error"]["code"] == "plan_open"
    assert out["error"]["details"]["status"] == status
    assert h.log == []  # no prepare, no snapshot
    # Read-only tools still work.
    assert await h.call(h.read_spec()) == {"ok": True}
    assert h.log == ["read"]


async def test_running_outside_step_refused(h: Harness) -> None:
    plan = await h.store(make_plan())
    await h.set_status(plan, "running")
    out = await h.call(h.spec())
    assert out["error"]["code"] == "plan_step_required"
    assert "plan_step_begin" in out["error"]["suggestion"]
    assert h.log == []


async def test_running_step_of_another_turn_refused(h: Harness) -> None:
    await h.running()
    h.run_id = "run-other"  # a later turn has no active step
    out = await h.call(h.spec())
    assert out["error"]["code"] == "plan_step_required"


async def test_inside_step_counts_against_expected_not_turn_budget(h: Harness) -> None:
    await h.running()
    out = await h.call(h.spec(changes=4))
    assert out["ok"] is True
    assert h.state.step_used == 4 and h.state.logical_used == 0
    assert out["budget"] == {"used": 4, "limit": 15, "scope": "step", "step": 1}
    assert h.log == ["prepare", "on_mutation", "apply"]


async def test_step_tolerance_50_percent(h: Harness) -> None:
    await h.running()  # step 1 expects 10 -> limit 15
    assert "ok" in await h.call(h.spec(changes=15))
    h.log.clear()
    out = await h.call(h.spec(changes=1))
    err = out["error"]
    assert err["code"] == "plan_step_exceeded"
    assert err["details"] == {"used": 15, "needed": 16, "limit": 15}
    assert "plan_revise" in err["suggestion"]
    assert h.log == ["prepare"]  # refused before the snapshot
    plan = await h.plan()
    assert plan.status == "paused" and plan.pause_reason == "step_exceeded"
    step = plan.step(1)
    assert step.status == "failed" and "16" in (step.failure or "")
    assert h.state.active_step_id is None
    # The paused plan blocks every further edit.
    assert (await h.call(h.spec()))["error"]["code"] == "plan_open"


async def test_small_step_has_at_least_one_spare_change(h: Harness) -> None:
    await h.running(expected=(1,))
    assert "ok" in await h.call(h.spec(changes=2))
    assert (await h.call(h.spec(changes=1)))["error"]["code"] == "plan_step_exceeded"


async def test_turn_budget_refusal_suggests_plan_propose(h: Harness) -> None:
    assert "ok" in await h.call(h.spec(changes=20))
    out = await h.call(h.spec(changes=1))
    assert out["error"]["code"] == "plan_required"
    assert "plan_propose" in out["error"]["suggestion"]
    assert out["error"]["details"]["limit"] == 20


async def test_closed_plan_falls_back_to_budget(h: Harness) -> None:
    plan = await h.store(make_plan())
    await h.plans.repo.save(machine.transition(plan, "reject", now=NOW))
    assert "ok" in await h.call(h.spec(changes=3))
    assert h.state.logical_used == 3


async def test_policy_always_without_step_refused(h: Harness) -> None:
    await h.settings.put(PID, plan_policy="always")
    out = await h.call(h.spec())
    assert out["error"]["code"] == "plan_step_required"
    assert h.log == []
    await h.running()
    assert "ok" in await h.call(h.spec())  # inside a step: allowed


async def test_step_ends_turn_budget_resumes_after_done(h: Harness) -> None:
    await h.running(expected=(10,))
    await h.call(h.spec(changes=8))
    h.plans._clear_step(h.state)  # as plan_step_done does
    await h.set_status(await h.plan(), "done")
    assert "ok" in await h.call(h.spec(changes=20))  # turn budget untouched by the step
    assert h.state.logical_used == 20
