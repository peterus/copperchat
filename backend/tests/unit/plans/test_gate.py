"""PlanGate (E8b Task 2): open plans, steps, step tolerance, turn budget, policy."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from copperchat.db.repos.conversations import ConversationRepo
from copperchat.plans import machine
from tests.unit.plans.conftest import NOW, PID, Harness, make_plan


@pytest.mark.parametrize("status", ["proposed", "approved", "paused"])
async def test_plan_open_refuses_mutation(h: Harness, status: str) -> None:
    plan = await h.store(make_plan())
    await h.set_status(plan, status)
    out = await h.call(h.spec())
    assert out["error"]["code"] == "plan_open"
    assert out["error"]["details"]["status"] == status
    hint = out["error"]["suggestion"]
    if status == "approved":  # nothing left to approve: begin the next step
        assert "approve or" not in hint and "plan_step_begin" in hint
    elif status == "proposed":
        assert "approve or reject" in hint
    else:
        assert "resume" in hint
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
    h.plans.clear_step(h.state)  # as plan_step_done does
    await h.set_status(await h.plan(), "done")
    assert "ok" in await h.call(h.spec(changes=20))  # turn budget untouched by the step
    assert h.state.logical_used == 20


async def test_stale_step_state_never_bypasses_the_turn_budget(h: Harness) -> None:
    plan = await h.running(expected=(30,))
    # The plan was paused meanwhile (e.g. by another path) but the turn kept its step.
    await h.plans.repo.save(machine.transition(plan, "interrupt", now=NOW))
    await h.set_status(await h.plan(), "aborted")
    assert h.state.active_step_id is not None
    out = await h.call(h.spec(changes=21))
    assert out["error"]["code"] == "plan_required"
    assert h.state.active_step_id is None


async def test_refund_only_for_symbols_placed_in_the_step(h: Harness) -> None:
    from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical, NetIntent
    from copperchat.tools import ToolContext, ToolResult, edit_spec
    from tests.unit.plans.conftest import SCHEMA, SHEET

    h.state.placed_this_turn.add("pre-step-sym")  # placed earlier in the turn
    await h.running(expected=(4,))

    def spec(changes: int, deleted: list[str], placed: list[str]) -> Any:
        async def prepare(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
            return ChangeSet("t", SHEET, [], {}, NetIntent(), Logical(changes, list(deleted)))

        async def apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
            cs.logical.placed_symbols = list(placed)
            return ToolResult(data={"ok": True})

        return edit_spec("t", "d", SCHEMA, prepare, apply)

    await h.call(spec(1, [], ["own"]))
    assert h.state.step_used == 1
    await h.call(spec(1, ["own"], []))  # own placement deleted: refund
    assert h.state.step_used == 0
    await h.call(spec(1, ["pre-step-sym"], []))  # not placed in this step: costs 1
    assert h.state.step_used == 1


async def test_step_exceeded_posts_needs_attention_row(h: Harness) -> None:
    plan = await h.running(expected=(2,))
    assert (await h.call(h.spec(changes=4)))["error"]["code"] == "plan_step_exceeded"
    [(text, meta)] = h.engine.errors
    assert meta == {"code": "plan_needs_attention", "planId": plan.id, "reason": "step_exceeded"}
    assert "step 1" in text and "needed 4 logical changes" in text


async def test_step_placements_never_refund_the_turn_budget(h: Harness) -> None:
    from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical, NetIntent
    from copperchat.tools import ToolContext, ToolResult, edit_spec
    from tests.unit.plans.conftest import SCHEMA, SHEET

    def spec(changes: int, deleted: list[str], placed: list[str]) -> Any:
        async def prepare(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
            return ChangeSet("t", SHEET, [], {}, NetIntent(), Logical(changes, list(deleted)))

        async def apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
            cs.logical.placed_symbols = list(placed)
            return ToolResult(data={"ok": True})

        return edit_spec("t", "d", SCHEMA, prepare, apply)

    await h.running(expected=(4,))
    await h.call(spec(1, [], ["step-sym"]))
    assert "step-sym" not in h.state.placed_this_turn
    h.plans.clear_step(h.state)
    await h.set_status(await h.plan(), "done")
    await h.call(spec(1, ["step-sym"], []))  # a pre-existing symbol for the turn budget
    assert h.state.logical_used == 1 and h.state.deleted_preexisting == 1


async def _owned_by_session_a(h: Harness) -> str:
    """A plan owned by 'Session A'; returns the id of another session B."""
    repo = ConversationRepo(h.db)
    a = await repo.create(PID, "schematic", "Session A")
    b = await repo.create(PID, "schematic", "Session B")
    plan = replace(make_plan(), conversation_id=a.id)
    await h.store(plan)
    return b.id


@pytest.mark.parametrize("status", ["proposed", "paused", "running"])
async def test_refusal_names_the_owning_session(h: Harness, status: str) -> None:
    """Sessions C5 (spec §5.4): a sch_* call from session B while session A's plan is
    open is refused, and the refusal names Session A."""
    b = await _owned_by_session_a(h)
    plan = await h.plans.repo.open_for(PID)
    assert plan is not None
    await h.set_status(plan, status)
    base = h.ctx
    h.ctx = lambda: replace(base(), conversation_id=b)  # type: ignore[method-assign]
    out = await h.call(h.spec())
    err = out["error"]
    assert err["code"] == ("plan_step_required" if status == "running" else "plan_open")
    assert "Session A" in err["what"] and "Session A" in err["suggestion"]
    assert err["details"]["owner"] == "Session A"
    assert h.log == []


async def test_own_plan_refusal_keeps_its_hint(h: Harness) -> None:
    await _owned_by_session_a(h)
    plan = await h.plans.repo.open_for(PID)
    assert plan is not None
    base = h.ctx
    h.ctx = lambda: replace(base(), conversation_id=plan.conversation_id)  # type: ignore[method-assign]
    out = await h.call(h.spec())
    assert out["error"]["details"]["owner"] is None
    assert "approve or reject" in out["error"]["suggestion"]
