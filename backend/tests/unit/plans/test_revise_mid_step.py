"""#13 (spec §9.2): `plan_revise` from a running step — the step pauses, its counters
are persisted with it and restored when it resumes in a later run (even after a server
restart); rejecting such a revision falls back to the previous revision, paused."""

from __future__ import annotations

from typing import Any

import pytest

from copperchat.db import Database
from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical, NetIntent
from copperchat.plans.service import step_limit
from copperchat.tools import ToolContext, ToolResult, ToolSpec, edit_spec
from tests.unit.plans.conftest import PID, SCHEMA, SHEET, Harness, make_plan, step_spec


def placing(changes: int, deleted: list[str], placed: list[str]) -> ToolSpec:
    async def prepare(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
        return ChangeSet("t", SHEET, [], {}, NetIntent(), Logical(changes, list(deleted)))

    async def apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
        cs.logical.placed_symbols = list(placed)
        return ToolResult(data={"ok": True})

    return edit_spec("t", "d", SCHEMA, prepare, apply)


async def _used_3(h: Harness, expected: tuple[int, ...] = (4, 4)) -> None:
    """Step 1 (expected 4) begun; 3 logical changes used, one of them placing 'own'."""
    await h.running(expected=expected)
    assert "ok" in await h.call(placing(1, [], ["own"]))
    assert "ok" in await h.call(h.spec(changes=2))
    assert h.state.step_used == 3


async def _revise(h: Harness, expected: int = 8) -> Any:
    return await h.plans.revise(h.ctx(), [step_spec("bigger", expected), step_spec("b")], "why")


async def _approve_in_new_run(h: Harness, run: str = "run2") -> None:
    plan = await h.plan()
    await h.plans.approve(h.project(), plan.id)
    h.run_id = run
    await h.plans.step_begin(h.ctx(), 1)


async def test_counters_are_persisted_with_every_step_edit(h: Harness) -> None:
    await _used_3(h)
    step = (await h.plan()).step(1)
    assert (step.used, step.placed) == (3, ("own",))


async def test_revise_mid_step_pauses_the_step(h: Harness) -> None:
    await _used_3(h)
    revised = await _revise(h)
    assert revised.status == "proposed" and revised.revision == 2
    assert revised.step(1).status == "paused" and revised.step(1).used == 3
    assert h.state.active_step_id is None  # no edit until the revision is approved
    assert (await h.call(h.spec()))["error"]["code"] == "plan_open"
    [(text, meta)] = [(t, m) for t, m in h.engine.notices if m and m["action"] == "plan-revised"]
    assert meta["agentReason"] == "why" and "paused" in text


async def test_revised_step_inherits_used_count(h: Harness) -> None:
    await _used_3(h)
    await _revise(h, expected=8)
    await _approve_in_new_run(h)
    state = h.editing.turn(PID, "run2")
    assert state.step_used == 3 and state.step_expected == 8
    out = await h.call(h.spec(changes=1))
    assert out["budget"] == {"used": 4, "limit": step_limit(8), "scope": "step", "step": 1}


async def test_step_counters_survive_new_run(h: Harness, db: Database, tmp_path: Any) -> None:
    """Review Focus 2: a server restart between the revision and its approval (fresh
    services, no in-memory turn state) keeps the used count and the ledger."""
    await _used_3(h)
    await _revise(h, expected=8)
    fresh = Harness(tmp_path, db)
    await _approve_in_new_run(fresh)
    state = fresh.editing.turn(PID, "run2")
    assert state.step_used == 3 and state.step_placed == {"own"}
    # deleting the step's own placement still refunds it
    assert "ok" in await fresh.call(placing(1, ["own"], []))
    assert state.step_used == 2


async def test_reject_mid_step_revision_falls_back_paused(h: Harness) -> None:
    await _used_3(h)
    before = await h.plan()
    await _revise(h)
    back = await h.plans.reject(h.project(), before.id)
    assert back.status == "paused" and back.revision == 1
    assert [(s.id, s.title, s.expected_changes) for s in back.steps] == [
        (s.id, s.title, s.expected_changes) for s in before.steps
    ]
    assert back.step(1).status == "paused" and back.step(1).used == 3
    restored = [m for _t, m in h.engine.notices if m and m["action"] == "plan-restored"]
    assert restored == [{"action": "plan-restored", "planId": back.id, "revision": 1}]
    assert not [m for _t, m in h.engine.notices if m and m["action"] == "plan-rejected"]
    # resume continues the interrupted step with its counters
    await h.plans.resume(h.project(), back.id)
    h.run_id = "run3"
    await h.plans.step_begin(h.ctx(), 1)
    state = h.editing.turn(PID, "run3")
    assert state.step_used == 3 and state.step_expected == 4


async def test_reject_of_a_fresh_plan_still_ends_it(h: Harness) -> None:
    plan = await h.store(make_plan())
    closed = await h.plans.reject(h.project(), plan.id)
    assert closed.status == "rejected"


async def test_exceeding_still_hard_stops(h: Harness) -> None:
    await _used_3(h)
    await _revise(h, expected=2)  # limit 3: the step has no room left
    await _approve_in_new_run(h)
    out = await h.call(h.spec(changes=1))
    assert out["error"]["code"] == "plan_step_exceeded"
    assert out["error"]["details"] == {"used": 3, "needed": 4, "limit": 3}
    plan = await h.plan()
    assert plan.status == "paused" and plan.step(1).status == "failed"


async def test_interrupted_step_resumes_with_its_counters(h: Harness) -> None:
    """A step interrupted by the turn end keeps its used count on resume (the edits are
    on disk); a failed step starts over as before."""
    await _used_3(h)
    await h.plans.pause(h.project(), "turn_ended")
    plan = await h.plan()
    assert plan.step(1).status == "interrupted"
    await h.plans.resume(h.project(), plan.id)
    h.run_id = "run2"
    await h.plans.step_begin(h.ctx(), 1)
    assert h.editing.turn(PID, "run2").step_used == 3


async def test_failed_step_restarts_its_counters(h: Harness) -> None:
    await _used_3(h)
    assert (await h.call(h.spec(changes=5)))["error"]["code"] == "plan_step_exceeded"
    plan = await h.plan()
    await h.plans.resume(h.project(), plan.id)
    h.run_id = "run2"
    await h.plans.step_begin(h.ctx(), 1)
    assert h.editing.turn(PID, "run2").step_used == 0
    assert (await h.plan()).step(1).used == 0


async def test_revise_mid_step_validates_before_pausing(h: Harness) -> None:
    await _used_3(h)
    with pytest.raises(StructuredToolError):
        await h.plans.revise(h.ctx(), [], "why")
    plan = await h.plan()
    assert plan.status == "running" and h.state.active_step_id is not None
