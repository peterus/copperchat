"""PlanService and plan tools (E8b Task 3) on the fake api-server with real history,
snapshots and SQLite; ERC runs are faked (E8a's turn-end ERC tests do the same)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from copperchat.agent.engine import ChainedHooks, TurnOutcome
from copperchat.changes import erc
from copperchat.changes.repo import ChangeEntry
from copperchat.db.repos.agent_state import RunRepo
from copperchat.db.repos.conversations import ConversationRepo
from copperchat.db.repos.editing_settings import EditingSettingsRepo
from copperchat.editing.hooks import EditingHooks
from copperchat.editing.service import EditingService
from copperchat.errors import StructuredToolError
from copperchat.plans import machine
from copperchat.plans.gate import PlanGate
from copperchat.plans.model import Plan
from copperchat.plans.service import PlanService
from copperchat.tools import ToolContext
from tests.support.history import FakeRuntime, replace_with_kicad_file
from tests.unit.history.conftest import Env, Opened, env, opened  # noqa: F401 (fixtures)
from tests.unit.history.test_history_hooks import done, turn
from tests.unit.plans.conftest import NOW, step_spec

U1 = "11111111-1111-1111-1111-111111111111"
U2 = "22222222-2222-2222-2222-222222222222"
RUN = "run-p1"


def violation(type_: str, uuid: str = U1, severity: str = "error") -> dict[str, Any]:
    return {
        "type": type_,
        "severity": severity,
        "description": f"{type_} on R1",
        "position_mm": {"x": 1.0, "y": 2.0},
        "items": [{"uuid": uuid, "description": "R1", "position_mm": None}],
        "sheet": "/",
    }


def vid(v: dict[str, Any]) -> str:
    return erc.violation_id(v)


@dataclass
class Rig:
    opened: Opened
    editing: EditingService
    plans: PlanService
    hooks: ChainedHooks
    current: list[dict[str, Any]] = field(default_factory=list)
    baselines: list[str] = field(default_factory=list)
    mutations: list[str] = field(default_factory=list)
    run_id: str = RUN

    @property
    def env(self) -> Env:
        return self.opened.env

    @property
    def pid(self) -> str:
        return self.opened.project.id

    def ctx(self) -> ToolContext:
        run_id = self.run_id
        t = turn(run_id)

        async def on_mutation() -> None:
            if run_id not in self.mutations:  # the engine takes one snapshot per turn
                self.mutations.append(run_id)
                await self.hooks.on_first_mutation(t)

        return ToolContext(
            project=self.opened.project,
            session=self.env.sessions.current() or self.opened.session,
            settings=self.env.settings,
            run_id=run_id,
            on_mutation=on_mutation,
            editing=self.editing,
        )

    @property
    def state(self) -> Any:
        return self.editing.turn(self.pid, self.run_id)

    def mutate(self) -> None:
        """A two-phase tool applied (the next ERC is not reused)."""
        self.state.call_seq += 1
        self.state.last_mutation_seq = self.state.call_seq

    def render(self) -> None:
        self.editing.note_call(self.ctx(), "render_view", {"doc": "sch"})

    async def last_plan(self) -> Plan:
        row = await self.editing.db.fetchone(
            "SELECT id FROM plans WHERE project_id = ? ORDER BY rowid DESC LIMIT 1", (self.pid,)
        )
        assert row is not None
        return await self.plans.repo.get(self.pid, str(row["id"]))

    async def proposed(self, n: int = 2) -> Plan:
        return await self.plans.propose(
            self.ctx(), "LED circuit", "R + LED", [step_spec(f"s{i}", 5) for i in range(n)]
        )

    async def approved(self, n: int = 2) -> Plan:
        plan = await self.proposed(n)
        approved = machine.transition(plan, "approve", now=NOW)
        await self.plans.repo.save(approved)
        return approved

    async def begun(self, n: int = 2) -> Plan:
        await self.approved(n)
        await self.plans.step_begin(self.ctx(), 1)
        return await self.last_plan()

    def notices(self, action: str) -> list[tuple[str, dict[str, Any]]]:
        return [(t, m) for t, m in self.env.engine.notices if m and m.get("action") == action]


@pytest.fixture
def rig(opened: Opened, monkeypatch: pytest.MonkeyPatch) -> Rig:  # noqa: F811
    db = opened.env.svc.db
    editing = EditingService(
        db=db, broadcaster=None, settings_repo=EditingSettingsRepo(db), history=opened.env.svc
    )
    plans = PlanService(
        db=db,
        editing=editing,
        history=opened.env.svc,
        runtime=FakeRuntime(opened.env.engine),
        broadcaster=opened.env.broadcaster,
    )
    editing.gate = PlanGate(editing, plans)
    editing.plans = plans
    opened.env.svc.on_external_change.append(plans.on_external_change)
    hooks = ChainedHooks(
        [opened.hooks, EditingHooks(editing, opened.project, opened.env.engine)]  # type: ignore[arg-type]
    )
    r = Rig(opened, editing, plans, hooks)

    async def baseline(svc: Any, project: Any, snap: Any) -> list[dict[str, Any]]:
        r.baselines.append(snap.id)
        return []

    async def runner(session: Any, doc: str) -> list[dict[str, Any]]:
        return list(r.current)

    monkeypatch.setattr(erc, "erc_baseline", baseline)
    monkeypatch.setattr(erc, "check_violations", runner)
    return r


async def expect_error(coro: Any, code: str) -> StructuredToolError:
    with pytest.raises(StructuredToolError) as err:
        await coro
    assert err.value.code == code, err.value.to_dict()
    return err.value


# -- propose / revise -----------------------------------------------------------------


async def test_propose_posts_plan_notice_and_frame(rig: Rig) -> None:
    plan = await rig.proposed()
    assert plan.status == "proposed" and plan.revision == 1 and len(plan.steps) == 2
    [(text, meta)] = rig.notices("plan")
    assert meta == {"action": "plan", "planId": plan.id} and "LED" not in text
    frame = rig.env.broadcaster.frames[-1]
    assert frame.type == "plan-updated" and frame.plan.id == plan.id
    assert frame.plan.steps[0].expected_changes == 5


async def test_plan_is_owned_by_the_proposing_runs_session(rig: Rig) -> None:
    db = rig.editing.db
    other = (await ConversationRepo(db).create(rig.pid, "free", None)).id
    await RunRepo(db).start(rig.run_id, rig.pid, other)
    plan = await rig.proposed()
    assert plan.conversation_id == other, "the run's session, not the engine's default"
    assert rig.env.engine.notice_conversations[-1] == other
    await rig.plans.on_external_change(rig.opened.project, ["x.kicad_sch"])  # no-op: proposed
    approved = await rig.plans.repo.save(machine.transition(plan, "approve", now=NOW))
    await rig.plans.on_external_change(rig.opened.project, ["x.kicad_sch"])
    assert approved.conversation_id == other
    assert rig.env.engine.notice_conversations[-1] == other
    assert rig.env.engine.note_conversations[-1] == other


async def test_propose_refused_while_open(rig: Rig) -> None:
    await rig.proposed()
    err = await expect_error(rig.proposed(), "plan_open")
    assert "plan_revise" in err.suggestion


async def test_propose_validates_steps(rig: Rig) -> None:
    ctx = rig.ctx()
    await expect_error(rig.plans.propose(ctx, "t", "s", []), "invalid_argument")
    bad = step_spec()
    bad["expected_changes"] = 0
    await expect_error(rig.plans.propose(ctx, "t", "s", [bad]), "invalid_argument")
    await expect_error(rig.plans.propose(ctx, "", "s", [step_spec()]), "invalid_argument")
    many = [step_spec() for _ in range(31)]
    await expect_error(rig.plans.propose(ctx, "t", "s", many), "invalid_argument")
    assert await rig.plans.repo.open_for(rig.pid) is None


async def test_revise_only_proposed_or_paused(rig: Rig) -> None:
    plan = await rig.proposed()
    revised = await rig.plans.revise(rig.ctx(), [step_spec("only")], "user asked")
    assert revised.revision == 2 and [s.title for s in revised.steps] == ["only"]
    await rig.plans.repo.save(machine.transition(revised, "approve", now=NOW))
    await expect_error(rig.plans.revise(rig.ctx(), [step_spec()], "x"), "plan_state")
    assert plan.id == revised.id


async def test_revise_paused_keeps_finished_steps(rig: Rig) -> None:
    await rig.begun(3)
    rig.render()
    await rig.plans.step_done(rig.ctx(), 1, "placed")
    await rig.plans.pause(rig.opened.project, "turn_ended")
    revised = await rig.plans.revise(rig.ctx(), [step_spec("new")], "other approach")
    assert [(s.idx, s.status) for s in revised.steps] == [(1, "done"), (2, "pending")]
    assert revised.status == "proposed" and revised.steps[0].checkpoint_id is not None


# -- step begin / done ------------------------------------------------------------------


async def test_step_begin_1_takes_base_snapshot(rig: Rig) -> None:
    plan = await rig.begun()
    assert rig.mutations == [RUN]  # on_mutation called once
    turn_snap = await rig.env.svc.store(rig.opened.project).latest_for_run(RUN)
    assert turn_snap is not None and plan.base_snapshot_id == turn_snap.id
    assert plan.status == "running" and plan.steps[0].status == "running"
    assert rig.state.active_step_id == plan.steps[0].id and rig.state.step_expected == 5


async def test_step_begin_refused_before_approval_and_read_only(rig: Rig) -> None:
    await rig.proposed()
    await expect_error(rig.plans.step_begin(rig.ctx(), 1), "plan_state")
    assert rig.mutations == []
    rig.opened.session.read_only = True
    await expect_error(rig.plans.step_begin(rig.ctx(), 1), "read_only")


async def test_step_done_refused_with_unjustified_error(rig: Rig) -> None:
    await rig.begun()
    rig.current = [violation("pin_not_connected")]
    err = await expect_error(rig.plans.step_done(rig.ctx(), 1, "placed R1"), "erc_errors_open")
    assert err.details is not None and err.details["count"] == 1
    [listed] = err.details["errors"]
    assert listed["id"] == vid(rig.current[0]) and listed["type"] == "pin_not_connected"
    plan = await rig.last_plan()
    assert plan.status == "running" and plan.steps[0].status == "running"
    assert plan.steps[0].checkpoint_id is None


async def test_step_done_accepts_justification(rig: Rig) -> None:
    await rig.begun()
    v = violation("pin_not_connected")
    rig.current = [v]
    rig.render()
    out = await rig.plans.step_done(
        rig.ctx(), 1, "placed R1", {vid(v): "the connector follows in step 2"}
    )
    assert out["erc"]["justified"] == [vid(v)] and out["erc"]["new_errors"] == 1
    plan = await rig.last_plan()
    assert plan.steps[0].status == "done"
    dto = await rig.plans.dto(plan)
    assert dto["steps"][0]["erc"] == {
        "errors": 1,
        "warnings": 0,
        "justifications": {vid(v): "the connector follows in step 2"},
        "resultId": plan.steps[0].erc_result_id,
    }


async def test_step_done_after_fix_accepts(rig: Rig) -> None:
    await rig.begun()
    rig.current = [violation("pin_not_connected")]
    await expect_error(rig.plans.step_done(rig.ctx(), 1, "x"), "erc_errors_open")
    rig.current = []
    rig.mutate()  # the agent fixed it
    out = await rig.plans.step_done(rig.ctx(), 1, "fixed")
    assert out["status"] == "done" and out["next_step"] == 2
    assert rig.state.active_step_id is None


async def test_second_erc_refusal_pauses_plan(rig: Rig) -> None:
    plan = await rig.begun()
    rig.current = [violation("pin_not_connected"), violation("wire_dangling", U2, "warning")]
    await expect_error(rig.plans.step_done(rig.ctx(), 1, "x"), "erc_errors_open")
    err = await expect_error(rig.plans.step_done(rig.ctx(), 1, "x"), "erc_errors_open")
    assert err.details is not None and err.details["plan_paused"] is True
    paused = await rig.last_plan()
    assert paused.status == "paused" and paused.pause_reason == "erc_regression"
    step = paused.steps[0]
    assert step.status == "failed" and "pin_not_connected" in (step.failure or "")
    row = await rig.editing.erc_results.get(rig.pid, step.erc_result_id or "")
    assert len(row["new_errors"]) == 1 and len(row["new_warnings"]) == 1
    [(_text, meta)] = rig.env.engine.errors
    assert meta == {
        "code": "plan_needs_attention",
        "planId": plan.id,
        "ercResultId": step.erc_result_id,
    }
    assert rig.state.active_step_id is None


async def test_different_errors_refuse_again_without_pausing(rig: Rig) -> None:
    await rig.begun()
    rig.current = [violation("pin_not_connected")]
    await expect_error(rig.plans.step_done(rig.ctx(), 1, "x"), "erc_errors_open")
    rig.current = [violation("pin_not_connected", U2)]
    rig.mutate()
    err = await expect_error(rig.plans.step_done(rig.ctx(), 1, "x"), "erc_errors_open")
    assert err.details is not None and "plan_paused" not in err.details
    assert (await rig.last_plan()).status == "running"


async def test_step_done_takes_checkpoint_and_counts_changes(rig: Rig) -> None:
    step_id = (await rig.approved()).steps[0].id

    def entry(uuid: str, run_id: str = RUN, step: str | None = step_id) -> ChangeEntry:
        return ChangeEntry(rig.pid, run_id, step, 1, "sch_add_wire", "add", "wire", uuid, "/r")

    await rig.editing.changes.add_many([entry("old", run_id="before")])  # an older attempt
    await rig.plans.step_begin(rig.ctx(), 1)
    plan = await rig.last_plan()
    # rows of the step in any run since it began (a resumed step finishes in another run)
    await rig.editing.changes.add_many(
        [entry("a"), entry("b"), entry("c", step=None), entry("d", run_id="earlier")]
    )
    rig.render()
    out = await rig.plans.step_done(rig.ctx(), 1, "wired")
    assert out["change_count"] == 3
    snap = await rig.env.svc.store(rig.opened.project).get(out["checkpoint_id"])
    assert snap.kind == "step" and snap.plan_step_id == step_id and snap.label == "Plan step 1"
    done_plan = await rig.last_plan()
    assert done_plan.steps[0].checkpoint_id == snap.id and done_plan.steps[0].change_count == 3
    assert done_plan.steps[0].summary == "wired"
    # Step 1's ERC baseline is the plan's base snapshot.
    assert rig.baselines == [plan.base_snapshot_id]
    # Step 2 is measured against step 1's checkpoint; the last step finishes the plan.
    await rig.plans.step_begin(rig.ctx(), 2)
    rig.render()
    last = await rig.plans.step_done(rig.ctx(), 2, "done")
    assert rig.baselines[-1] == snap.id
    assert last["plan_status"] == "done" and last["next_step"] is None


async def test_step_done_without_render_adds_reminder(rig: Rig) -> None:
    await rig.begun(3)
    out = await rig.plans.step_done(rig.ctx(), 1, "x")
    assert any("render_view" in n for n in out["notes"])
    await rig.plans.step_begin(rig.ctx(), 2)
    rig.render()
    out = await rig.plans.step_done(rig.ctx(), 2, "x")
    assert not any("render_view" in n for n in out["notes"])
    await rig.editing.settings_repo.put(rig.pid, show_agent_view=False)
    await rig.plans.step_begin(rig.ctx(), 3)
    out = await rig.plans.step_done(rig.ctx(), 3, "x")
    assert out["notes"] == []


async def test_step_done_of_a_step_begun_in_another_turn_refused(rig: Rig) -> None:
    await rig.begun()
    rig.run_id = "run-other"
    await expect_error(rig.plans.step_done(rig.ctx(), 1, "x"), "plan_state")


async def test_unknown_justification_ids_are_reported(rig: Rig) -> None:
    await rig.begun()
    rig.render()
    out = await rig.plans.step_done(rig.ctx(), 1, "x", {"deadbeef0000": "why"})
    assert any("deadbeef0000" in n for n in out["notes"])
    await expect_error(rig.plans.step_done(rig.ctx(), 2, "x", {"a": ""}), "invalid_argument")


# -- pauses -------------------------------------------------------------------------


async def test_turn_end_pauses_running_plan(rig: Rig) -> None:
    await rig.begun()
    await rig.hooks.on_turn_end(turn(RUN), done())
    plan = await rig.last_plan()
    assert plan.status == "paused" and plan.pause_reason == "turn_ended"
    assert plan.steps[0].status == "interrupted"
    [(_, meta)] = rig.notices("plan-paused")
    assert meta["reason"] == "turn_ended" and meta["planId"] == plan.id
    assert rig.editing.peek_turn(rig.pid, RUN) is None  # turn state dropped


async def test_interrupted_turn_marks_step_interrupted(rig: Rig) -> None:
    await rig.begun()
    await rig.hooks.on_turn_end(turn(RUN), done(reason="interrupted"))
    plan = await rig.last_plan()
    assert plan.pause_reason == "interrupted" and plan.steps[0].status == "interrupted"


@pytest.mark.parametrize(
    ("outcome", "reason", "step_status"),
    [
        (
            TurnOutcome(reason="error", document_inconsistent=True),
            "document_inconsistent",
            "failed",
        ),
        (TurnOutcome(reason="rate_limited"), "rate_limited", "interrupted"),
    ],
)
async def test_turn_end_mapping_of_other_outcomes(
    rig: Rig, outcome: TurnOutcome, reason: str, step_status: str
) -> None:
    await rig.begun()
    await rig.plans.on_turn_end(rig.opened.project, RUN, outcome, rig.env.engine)
    plan = await rig.last_plan()
    assert plan.status == "paused" and plan.pause_reason == reason
    assert plan.steps[0].status == step_status


async def test_kicad_failed_marks_step_failed(rig: Rig) -> None:
    await rig.begun()
    await rig.hooks.on_turn_end(turn(RUN), done("sch", reason="error", failed=True))
    plan = await rig.last_plan()
    assert plan.status == "paused" and plan.pause_reason == "kicad_failed"
    assert plan.steps[0].status == "failed" and plan.steps[0].failure


async def test_turn_end_leaves_approved_and_proposed_plans(rig: Rig) -> None:
    await rig.approved()
    await rig.hooks.on_turn_end(turn(RUN), done())
    assert (await rig.last_plan()).status == "approved"


async def test_external_change_pauses_plan(rig: Rig) -> None:
    await rig.approved()
    project = rig.opened.project
    assert project.pcb_file is not None
    from tests.support.history import other_board

    replace_with_kicad_file(project.pcb_file, other_board())
    await rig.hooks.on_turn_start(turn("run-2"))
    plan = await rig.last_plan()
    assert plan.status == "paused" and plan.pause_reason == "external_change"
    [(text, _)] = rig.notices("plan-paused")
    assert "base changed" in text
    assert any(plan.id in n and "outside CopperChat" in n for n in rig.env.engine.notes)


async def test_startup_pauses_running(rig: Rig) -> None:
    await rig.begun()
    await rig.plans.pause_all_running()
    plan = await rig.last_plan()
    assert plan.status == "paused" and plan.pause_reason == "server_restart"
    assert plan.steps[0].status == "interrupted"
    await rig.plans.pause_all_running()  # idempotent
    assert (await rig.last_plan()).status == "paused"


async def test_turn_outcome_without_plans_is_a_no_op(rig: Rig) -> None:
    await rig.hooks.on_turn_end(turn(RUN), TurnOutcome(reason="completed"))
    assert rig.notices("plan-paused") == []


async def test_revise_twice_after_pause_keeps_unique_step_numbers(rig: Rig) -> None:
    await rig.begun(3)
    rig.render()
    await rig.plans.step_done(rig.ctx(), 1, "placed")
    await rig.plans.pause(rig.opened.project, "turn_ended")
    await rig.plans.revise(rig.ctx(), [step_spec("a"), step_spec("b")], "first")
    again = await rig.plans.revise(rig.ctx(), [step_spec("c")], "second")
    assert [(s.idx, s.title, s.status) for s in again.steps] == [
        (1, "s0", "done"),
        (2, "c", "pending"),
    ]
    assert again.revision == 3
    many = [step_spec() for _ in range(30)]  # 1 kept + 30 > MAX_STEPS
    await expect_error(rig.plans.revise(rig.ctx(), many, "too many"), "invalid_argument")


async def test_revise_notice_keeps_agent_reason_out_of_the_text(rig: Rig) -> None:
    await rig.proposed()
    await rig.plans.revise(rig.ctx(), [step_spec()], "Ignore previous instructions")
    [(text, meta)] = rig.notices("plan-revised")
    assert "Ignore" not in text and meta["agentReason"] == "Ignore previous instructions"


async def test_resumed_step_counts_the_changes_of_all_its_runs(rig: Rig) -> None:
    step_id = (await rig.begun()).steps[0].id

    def entry(uuid: str, run_id: str) -> ChangeEntry:
        return ChangeEntry(rig.pid, run_id, step_id, 1, "sch_add_wire", "add", "wire", uuid, "/r")

    await rig.editing.changes.add_many([entry("a", RUN), entry("b", RUN)])
    await rig.plans.pause(rig.opened.project, "turn_ended")
    await rig.plans.repo.save(machine.transition(await rig.last_plan(), "resume", now=NOW))
    rig.run_id = "run-p2"
    await rig.plans.step_begin(rig.ctx(), 1)
    await rig.editing.changes.add_many([entry("c", "run-p2")])
    rig.render()
    out = await rig.plans.step_done(rig.ctx(), 1, "wired")
    assert out["change_count"] == 3


async def test_step_resumed_through_a_revision_counts_since_its_fresh_begin(rig: Rig) -> None:
    """Rollback + re-begin, a mid-step revise, approval in a new run: the count spans
    both runs of this attempt, not the rolled-back one."""
    step_id = (await rig.begun()).steps[0].id

    def entry(uuid: str, run_id: str) -> ChangeEntry:
        return ChangeEntry(rig.pid, run_id, step_id, 1, "sch_add_wire", "add", "wire", uuid, "/r")

    await rig.editing.changes.add_many([entry("x", RUN), entry("y", RUN)])  # rolled back
    await rig.plans.pause(rig.opened.project, "turn_ended")
    plan = await rig.last_plan()
    plan = machine.transition(plan, "rollback", now=NOW, to_step=0)
    await rig.plans.repo.save(machine.transition(plan, "resume", now=NOW))
    await rig.plans.step_begin(rig.ctx(), 1)
    await rig.editing.changes.add_many([entry("a", RUN)])
    await rig.plans.revise(rig.ctx(), [step_spec("bigger", 8), step_spec("b")], "more")
    await rig.plans.repo.save(machine.transition(await rig.last_plan(), "approve", now=NOW))
    rig.run_id = "run-p2"
    await rig.plans.step_begin(rig.ctx(), 1)
    await rig.editing.changes.add_many([entry("b", "run-p2")])
    rig.render()
    out = await rig.plans.step_done(rig.ctx(), 1, "wired")
    assert out["change_count"] == 2
