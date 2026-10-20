"""Plan state machine (spec §7.2) and repository (E8b Task 1)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from copperchat.db import Database
from copperchat.plans import machine
from copperchat.plans.model import InvalidPlanArgument, Plan, PlanStateError, to_dto
from copperchat.plans.repo import InvalidPlanId, PlanNotFound, PlanRepo
from tests.support.conversations import TEST_CID, ensure_test_conversation

NOW = "2026-10-18T10:00:00Z"
LATER = "2026-10-18T10:05:00Z"
PID = "a1b2c3d4e5f6"


def spec(title: str = "Place R1", expected: int = 2) -> dict[str, Any]:
    return {
        "title": title,
        "intent": "place the resistor",
        "expected_changes": expected,
        "touches": {"refs": ["R1"], "nets": []},
        "verification": "R1 on the sheet",
    }


def proposed(n: int = 2, plan_id: str = "f" * 32) -> Plan:
    return machine.new_plan(
        plan_id=plan_id,
        project_id=PID,
        title="LED circuit",
        summary="R + LED",
        steps=machine.build_steps(plan_id, [spec(f"step {i}") for i in range(1, n + 1)]),
        run_id="run-1",
        conversation_id=TEST_CID,
        now=NOW,
    )


def go(plan: Plan, event: str, **kw: Any) -> Plan:
    return machine.transition(plan, event, now=LATER, **kw)


def running(n: int = 2) -> Plan:
    return go(go(proposed(n), "approve"), "step_begin", idx=1, run_id="run-2")


def test_propose_creates_revision_1() -> None:
    plan = proposed()
    assert plan.status == "proposed" and plan.revision == 1 and plan.event_seq == 0
    assert [s.idx for s in plan.steps] == [1, 2]
    assert all(s.status == "pending" for s in plan.steps)


def test_reject_only_from_proposed() -> None:
    assert go(proposed(), "reject").status == "rejected"
    with pytest.raises(PlanStateError) as err:
        go(running(), "reject")
    assert err.value.code == "plan_state" and err.value.http_status == 409


def test_revise_from_proposed_bumps_revision() -> None:
    plan = go(proposed(), "revise", steps=machine.build_steps("f" * 32, [spec("only")]))
    assert plan.status == "proposed" and plan.revision == 2
    assert [s.title for s in plan.steps] == ["only"]


def test_revise_from_paused_returns_to_proposed() -> None:
    plan = go(running(3), "step_done", idx=1, checkpoint_id="c1", change_count=2)
    plan = go(plan, "turn_end")
    revised = go(plan, "revise", steps=machine.build_steps(plan.id, [spec("new")], start=2))
    assert revised.status == "proposed" and revised.revision == 2
    # The finished step stays (with its checkpoint); the rest is replaced.
    assert [(s.idx, s.title, s.status) for s in revised.steps] == [
        (1, "step 1", "done"),
        (2, "new", "pending"),
    ]
    assert revised.steps[0].checkpoint_id == "c1"


def test_revise_refused_while_running() -> None:
    with pytest.raises(PlanStateError):
        go(running(), "revise", steps=())


def test_approve_then_step_begin_runs() -> None:
    approved = go(proposed(), "approve")
    assert approved.status == "approved" and approved.event_seq == 1
    plan = go(approved, "step_begin", idx=1, run_id="run-2")
    assert plan.status == "running"
    assert plan.steps[0].status == "running" and plan.steps[0].run_id == "run-2"


def test_step_begin_out_of_order_refused() -> None:
    with pytest.raises(PlanStateError, match="cannot begin"):
        go(go(proposed(), "approve"), "step_begin", idx=2, run_id="r")
    with pytest.raises(PlanStateError, match="still running"):
        go(running(), "step_begin", idx=2, run_id="r")


def test_step_begin_before_approval_refused() -> None:
    with pytest.raises(PlanStateError):
        go(proposed(), "step_begin", idx=1, run_id="r")


def test_step_done_keeps_running_with_steps_left() -> None:
    plan = go(running(), "step_done", idx=1, checkpoint_id="c1", change_count=3, summary="ok")
    assert plan.status == "running"
    assert plan.steps[0].status == "done" and plan.steps[0].change_count == 3
    assert plan.next_step is not None and plan.next_step.idx == 2


def test_last_step_done_finishes() -> None:
    plan = go(running(1), "step_done", idx=1, checkpoint_id="c1")
    assert plan.status == "done"


def test_step_done_of_a_pending_step_refused() -> None:
    with pytest.raises(PlanStateError, match="not running"):
        go(running(), "step_done", idx=2)


def test_interrupt_pauses_with_step_interrupted() -> None:
    plan = go(running(), "interrupt")
    assert plan.status == "paused" and plan.pause_reason == "interrupted"
    assert plan.steps[0].status == "interrupted"


def test_turn_end_with_steps_left_pauses_turn_ended() -> None:
    plan = go(go(running(), "step_done", idx=1, checkpoint_id="c1"), "turn_end")
    assert plan.status == "paused" and plan.pause_reason == "turn_ended"
    assert [s.status for s in plan.steps] == ["done", "pending"]


def test_fail_marks_step_failed_with_reason() -> None:
    plan = go(running(), "fail", reason="step_exceeded", failure="16 of 15 changes")
    assert plan.status == "paused" and plan.pause_reason == "step_exceeded"
    assert plan.steps[0].status == "failed" and plan.steps[0].failure == "16 of 15 changes"


def test_external_change_pauses_approved_and_running() -> None:
    for plan in (go(proposed(), "approve"), running()):
        paused = go(plan, "external_change")
        assert paused.status == "paused" and paused.pause_reason == "external_change"
    with pytest.raises(PlanStateError):
        go(proposed(), "external_change")


def test_resume_returns_to_approved() -> None:
    plan = go(go(running(), "interrupt"), "resume")
    assert plan.status == "approved" and plan.event_seq == 2 and plan.pause_reason is None
    # The interrupted step is the next one to begin again.
    again = go(plan, "step_begin", idx=1, run_id="run-3")
    assert again.steps[0].status == "running" and again.steps[0].run_id == "run-3"


def test_rollback_marks_later_steps_pending() -> None:
    plan = go(running(3), "step_done", idx=1, checkpoint_id="c1", change_count=2)
    plan = go(plan, "step_begin", idx=2, run_id="run-2")
    plan = go(plan, "step_done", idx=2, checkpoint_id="c2", change_count=1, erc_result_id="e2")
    plan = go(plan, "turn_end")
    back = go(plan, "rollback", to_step=1)
    assert back.status == "paused" and back.pause_reason == "rolled_back"
    assert [(s.status, s.checkpoint_id) for s in back.steps] == [
        ("done", "c1"),
        ("pending", None),
        ("pending", None),
    ]
    assert back.steps[1].erc_result_id is None and back.steps[1].change_count == 0
    to_start = go(plan, "rollback", to_step=0)
    assert all(s.status == "pending" for s in to_start.steps)


def test_rollback_to_unfinished_step_refused() -> None:
    plan = go(running(), "interrupt")
    with pytest.raises(PlanStateError, match="roll back"):
        go(plan, "rollback", to_step=1)
    with pytest.raises(InvalidPlanArgument) as bad:
        go(plan, "rollback", to_step=7)
    assert bad.value.code == "invalid_argument" and bad.value.http_status == 400


def test_abort_terminal() -> None:
    for plan in (proposed(), go(proposed(), "approve"), go(running(), "interrupt")):
        aborted = go(plan, "abort")
        assert aborted.status == "aborted" and not aborted.is_open
    with pytest.raises(PlanStateError):
        go(go(proposed(), "abort"), "resume")


def test_invalid_transition_raises() -> None:
    with pytest.raises(PlanStateError, match="not allowed"):
        go(proposed(), "resume")
    with pytest.raises(PlanStateError):
        go(running(), "abort")  # stop the agent first
    with pytest.raises(PlanStateError):
        go(go(running(1), "step_done", idx=1), "step_begin", idx=1, run_id="r")
    with pytest.raises(ValueError):
        go(proposed(), "explode")


def test_dto_is_camel_case() -> None:
    dto = to_dto(running())
    assert dto["projectId"] == PID and dto["pauseReason"] is None
    assert dto["conversationId"] == TEST_CID
    step = dto["steps"][0]
    assert step["expectedChanges"] == 2 and step["changeCount"] == 0
    assert step["touches"] == {"refs": ["R1"], "nets": []} and step["erc"] is None


# -- repository -------------------------------------------------------------------


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[PlanRepo]:
    db = await Database.open(tmp_path / "db.sqlite")
    await ensure_test_conversation(db)
    yield PlanRepo(db)
    await db.close()


async def test_insert_get_and_save_roundtrip(repo: PlanRepo) -> None:
    plan = await repo.insert(proposed())
    assert await repo.get(PID, plan.id) == plan
    assert await repo.open_for(PID) == plan
    assert await repo.open_status(PID) == (plan.id, "proposed")
    nxt = await repo.save(go(go(plan, "approve"), "step_begin", idx=1, run_id="run-2"))
    assert await repo.get(PID, plan.id) == nxt
    assert [p.id for p in await repo.running()] == [plan.id]


async def test_second_open_plan_rejected(repo: PlanRepo) -> None:
    await repo.insert(proposed(plan_id="a" * 32))
    with pytest.raises(PlanStateError) as err:
        await repo.insert(proposed(plan_id="b" * 32))
    assert err.value.code == "plan_open"
    with pytest.raises(PlanNotFound):  # nothing of the refused plan was written
        await repo.get(PID, "b" * 32)


async def test_closed_plan_allows_a_new_one(repo: PlanRepo) -> None:
    first = await repo.insert(proposed(plan_id="a" * 32))
    await repo.save(go(first, "reject"))
    await repo.insert(proposed(plan_id="b" * 32))
    open_ = await repo.open_for(PID)
    assert open_ is not None and open_.id == "b" * 32


async def test_get_validates_ids(repo: PlanRepo) -> None:
    with pytest.raises(InvalidPlanId):
        await repo.get(PID, "../x")
    with pytest.raises(PlanNotFound):
        await repo.get(PID, "c" * 32)
    await repo.insert(proposed(plan_id="d" * 32))
    with pytest.raises(PlanNotFound):  # another project's plan is not found
        await repo.get("ffffffffffff", "d" * 32)


async def test_protected_snapshot_ids(repo: PlanRepo) -> None:
    from dataclasses import replace

    plan = go(running(), "step_done", idx=1, checkpoint_id="c" * 32)
    await repo.insert(replace(plan, base_snapshot_id="b" * 32))
    assert await repo.protected_snapshot_ids(PID) == {"b" * 32, "c" * 32}
