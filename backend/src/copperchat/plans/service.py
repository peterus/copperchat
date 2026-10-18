"""PlanService (E-C11, spec §7): plan tools, REST events, pauses and rollback.

Every state change goes through `machine.transition`, is written by `PlanRepo` under a
per-project lock and published as a `plan-updated` frame. Messages to the agent name
a plan only by id, revision and step (never its agent-authored title: prompt-injection
hardening).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from copperchat.changes import erc
from copperchat.changes.checkpoints import take_step_checkpoint
from copperchat.db import Database, utcnow_iso
from copperchat.errors import CopperChatError, StructuredToolError
from copperchat.history.store import SnapshotNotFound
from copperchat.kicad_session import KicadUnavailable
from copperchat.plans import machine
from copperchat.plans.model import (
    InvalidPlanArgument,
    Plan,
    PlanStateError,
    PlanStep,
    StepErc,
    to_dto,
)
from copperchat.plans.repo import PlanRepo
from copperchat.projects import Project

if TYPE_CHECKING:
    from copperchat.agent.engine import TurnOutcome
    from copperchat.editing.service import EditingService
    from copperchat.editing.state import EditTurnState
    from copperchat.history.service import HistoryService
    from copperchat.tools.spec import ToolContext

log = logging.getLogger(__name__)

MAX_STEPS = 30
TITLE_MAX = 120
SUMMARY_MAX = 2000
TEXT_MAX = 1000  # step intent / verification
EXPECTED_MAX = 200
TOUCH_MAX = 50  # refs / nets per step (the DTO is re-sent on every plan change)
TOUCH_ITEM_MAX = 64
REASON_MAX = 500
JUSTIFICATIONS_MAX = 100
ERRORS_LISTED = 20  # new ERC errors named in a refusal
_DESC_CHARS = 160


def step_limit(expected: int) -> int:
    """A step may use its expected changes + 50 % (at least one more)."""
    return max(expected + 1, expected * 3 // 2)


def _invalid(what: str, why: str, suggestion: str) -> StructuredToolError:
    return StructuredToolError(what, why, suggestion, code="invalid_argument")


def _text(value: Any, name: str, limit: int, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise _invalid(f"Invalid {name}", f"{name} must be a non-empty string", f"Pass {name}.")
    if len(value) > limit:
        raise _invalid(
            f"{name} is too long",
            f"{name} has {len(value)} characters (max {limit})",
            "Shorten it.",
        )
    return value.strip()


def validate_steps(raw: Any, *, minimum: int = 1, offset: int = 0) -> list[dict[str, Any]]:
    """Step specs of plan_propose / plan_revise; `offset` = finished steps kept."""
    if not isinstance(raw, list) or not (minimum <= len(raw) <= MAX_STEPS - offset):
        raise _invalid(
            "Invalid plan steps",
            f"steps must be a list of {minimum}-{MAX_STEPS - offset} steps",
            f"Split the work into at most {MAX_STEPS} steps; larger work needs several plans.",
        )
    out: list[dict[str, Any]] = []
    for i, s in enumerate(raw, start=1 + offset):
        if not isinstance(s, dict):
            raise _invalid(f"Invalid step {i}", "each step must be an object", "Fix the step.")
        expected = s.get("expected_changes")
        if (
            not isinstance(expected, int)
            or isinstance(expected, bool)
            or not (1 <= expected <= EXPECTED_MAX)
        ):
            raise _invalid(
                f"Invalid expected_changes in step {i}",
                f"expected_changes must be an integer 1-{EXPECTED_MAX} (logical changes)",
                "Count the step's logical changes (a placed part, a connection, a label per "
                "pin, ...); split larger steps.",
            )
        touches = s.get("touches") or {}
        if not isinstance(touches, dict) or set(touches) - {"refs", "nets"}:
            raise _invalid(
                f"Invalid touches in step {i}", "touches is {refs: [...], nets: [...]}", "Fix it."
            )
        clean: dict[str, list[str]] = {}
        for key in ("refs", "nets"):
            items = touches.get(key) or []
            if (
                not isinstance(items, list)
                or len(items) > TOUCH_MAX
                or not all(isinstance(x, str) and 0 < len(x) <= TOUCH_ITEM_MAX for x in items)
            ):
                raise _invalid(
                    f"Invalid touches.{key} in step {i}",
                    f"touches.{key} is a list of at most {TOUCH_MAX} short strings",
                    "Fix it.",
                )
            clean[key] = list(items)
        out.append(
            {
                "title": _text(s.get("title"), f"step {i} title", TITLE_MAX),
                "intent": _text(s.get("intent"), f"step {i} intent", TEXT_MAX),
                "expected_changes": expected,
                "touches": clean,
                "verification": _text(s.get("verification"), f"step {i} verification", TEXT_MAX),
            }
        )
    return out


def _brief(v: dict[str, Any]) -> dict[str, Any]:
    """A new ERC violation for the agent; the description quotes design text (data)."""
    return {
        "id": v.get("id"),
        "type": v.get("type"),
        "severity": v.get("severity"),
        "description": " ".join(str(v.get("description", "")).split())[:_DESC_CHARS],
        "sheet": v.get("sheet"),
        "position_mm": v.get("position_mm"),
    }


class PlanService:
    def __init__(
        self,
        *,
        db: Database,
        editing: EditingService,
        history: HistoryService | None,
        runtime: Any,
        broadcaster: Any | None,
    ) -> None:
        self.repo = PlanRepo(db)
        self.editing = editing
        self.history = history
        self.runtime = runtime
        self.broadcaster = broadcaster
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, project_id: str) -> asyncio.Lock:
        return self._locks.setdefault(project_id, asyncio.Lock())

    # ---- DTO / publishing
    async def dto(self, plan: Plan) -> dict[str, Any]:
        rows = await self.repo.erc_rows(plan)
        summaries = {
            sid: StepErc(
                errors=r["errors"],
                warnings=r["warnings"],
                justifications=r["justifications"],
                result_id=r["id"],
            )
            for sid, r in rows.items()
        }
        return to_dto(plan, summaries)

    async def _publish(self, plan: Plan) -> None:
        if self.broadcaster is None:
            return
        from copperchat.agent.protocol import PlanDTO, PlanUpdatedFrame

        try:
            frame = PlanUpdatedFrame(
                project_id=plan.project_id, plan=PlanDTO.model_validate(await self.dto(plan))
            )
            await self.broadcaster.publish(plan.project_id, frame)
        except Exception:  # the plan is saved; the card catches up on the next load
            log.exception("plan-updated for %s failed", plan.id)

    async def _save(self, plan: Plan) -> Plan:
        await self.repo.save(plan)
        await self._publish(plan)
        return plan

    def _engine(self, project_id: str) -> Any | None:
        return self.runtime.get(project_id) if self.runtime is not None else None

    async def _notice(self, project_id: str, text: str, meta: dict[str, Any]) -> None:
        engine = self._engine(project_id)
        if engine is not None:
            await engine.post_notice(text, meta)

    def _note(self, project_id: str, text: str) -> None:
        engine = self._engine(project_id)
        if engine is not None:
            engine.add_agent_note(text)

    @staticmethod
    def clear_step(state: EditTurnState) -> None:
        state.active_step_id = None
        state.step_idx = None
        state.step_expected = 0
        state.step_used = 0
        state.step_begin_seq = None
        state.step_placed = set()
        state.erc_refusal = None

    # ---- agent tools
    def _turn(self, ctx: ToolContext) -> EditTurnState:
        if ctx.run_id is None:
            raise StructuredToolError(
                "Plans need an agent turn", "there is no agent turn", "Call it during a turn."
            )
        return self.editing.turn(ctx.project.id, ctx.run_id)

    async def _open(self, project_id: str) -> Plan:
        plan = await self.repo.open_for(project_id)
        if plan is None:
            raise PlanStateError(
                "No plan is open",
                "There is no proposed, approved, running or paused plan",
                "Propose one with plan_propose.",
            )
        return plan

    async def propose(self, ctx: ToolContext, title: Any, summary: Any, steps: Any) -> Plan:
        self._turn(ctx)
        title = _text(title, "title", TITLE_MAX)
        summary = _text(summary, "summary", SUMMARY_MAX)
        specs = validate_steps(steps)
        pid = ctx.project.id
        async with self.lock(pid):
            if await self.repo.open_status(pid) is not None:
                raise PlanStateError(
                    "A plan is already open for this project",
                    "At most one plan can be open at a time",
                    "Change the open plan with plan_revise, or ask the user to finish, "
                    "reject or abort it first.",
                    code="plan_open",
                )
            plan_id = uuid.uuid4().hex
            plan = machine.new_plan(
                plan_id=plan_id,
                project_id=pid,
                title=title,
                summary=summary,
                steps=machine.build_steps(plan_id, specs),
                run_id=str(ctx.run_id),
                now=utcnow_iso(),
            )
            await self.repo.insert(plan)
        await self._publish(plan)
        await self._notice(
            pid,
            f"The agent proposed a plan with {len(plan.steps)} step(s). Approve or reject it.",
            {"action": "plan", "planId": plan.id},
        )
        return plan

    async def revise(self, ctx: ToolContext, steps: Any, reason: Any) -> Plan:
        self._turn(ctx)
        reason = _text(reason, "reason", REASON_MAX)
        pid = ctx.project.id
        async with self.lock(pid):
            plan = await self._open(pid)
            kept = plan.done_count  # finished steps always stay (machine "revise")
            specs = validate_steps(steps, offset=kept)
            new = machine.transition(
                plan,
                "revise",
                now=utcnow_iso(),
                steps=machine.build_steps(plan.id, specs, start=kept + 1),
            )
            await self._save(new)
        await self._notice(
            pid,
            f"The agent revised the plan (revision {new.revision}). Approve or reject it.",
            # The reason is agent-authored text: data for the plan card, not system voice.
            {
                "action": "plan-revised",
                "planId": new.id,
                "revision": new.revision,
                "agentReason": reason,
            },
        )
        return new

    async def step_begin(self, ctx: ToolContext, idx: Any) -> PlanStep:
        state = self._turn(ctx)
        if not isinstance(idx, int) or isinstance(idx, bool):
            raise _invalid("Invalid step", "step must be an integer", "Pass the step number.")
        if getattr(ctx.session, "read_only", False):
            raise StructuredToolError(
                "plan_step_begin cannot run on a read-only project",
                "The project is open in KiCad elsewhere (lock file present)",
                "Ask the user to close it there and reopen the project in CopperChat.",
                code="read_only",
            )
        pid = ctx.project.id
        async with self.lock(pid):
            plan = await self._open(pid)
            new = machine.transition(
                plan, "step_begin", now=utcnow_iso(), idx=idx, run_id=str(ctx.run_id)
            )
            if new.base_snapshot_id is None:
                # The pre-plan state: this turn's undo snapshot (taken now if the turn
                # has none yet), the target of "roll back to start".
                if ctx.on_mutation is not None:
                    await ctx.on_mutation()
                if self.history is not None:
                    snap = await self.history.store(ctx.project).latest_for_run(str(ctx.run_id))
                    if snap is not None:
                        new = replace(new, base_snapshot_id=snap.id)
            await self._save(new)
        step = new.step(idx)
        state.active_step_id = step.id
        state.step_idx = idx
        state.step_expected = step.expected_changes
        state.step_used = 0
        state.step_begin_seq = state.call_seq
        state.step_placed = set()
        state.erc_refusal = None
        return step

    async def _baseline(self, project: Project, plan: Plan, step: PlanStep, run_id: str) -> Any:
        """Snapshot the step's ERC delta is measured against: the previous step's
        checkpoint, the plan's base snapshot (step 1), else this turn's snapshot."""
        assert self.history is not None
        store = self.history.store(project)
        prev = next((s for s in plan.steps if s.idx == step.idx - 1), None)
        candidates = [prev.checkpoint_id if prev is not None else None, plan.base_snapshot_id]
        for sid in candidates:
            if sid is None:
                continue
            try:
                return await store.get(sid)
            except SnapshotNotFound:
                log.warning("ERC baseline snapshot %s of plan %s is gone", sid, plan.id)
        return await store.latest_for_run(run_id)

    async def _erc(
        self, ctx: ToolContext, plan: Plan, step: PlanStep, state: EditTurnState
    ) -> tuple[erc.ErcDelta, str | None]:
        empty = erc.ErcDelta([], [], [])
        if ctx.project.sch_file is None or self.history is None:
            return empty, None
        snap = await self._baseline(ctx.project, plan, step, str(ctx.run_id))
        if snap is None:
            return empty, None
        try:
            baseline = await erc.erc_baseline(self.history, ctx.project, snap)
            current = await erc.current_erc(ctx.session, state)
        except (StructuredToolError, KicadUnavailable):
            raise
        except Exception as exc:
            raise StructuredToolError(
                "The step's ERC check could not run",
                f"{type(exc).__name__}: {exc}",
                "Retry plan_step_done once; if it fails again, tell the user.",
            ) from exc
        return erc.erc_delta(baseline, current), snap.id

    @staticmethod
    def _justifications(raw: Any) -> dict[str, str]:
        if raw is None:
            return {}
        if (
            not isinstance(raw, dict)
            or len(raw) > JUSTIFICATIONS_MAX
            or not all(
                isinstance(k, str)
                and isinstance(v, str)
                and v.strip()
                and len(k) <= 64
                and len(v) <= REASON_MAX
                for k, v in raw.items()
            )
        ):
            raise _invalid(
                "Invalid erc_justifications",
                f"erc_justifications maps violation ids to reasons (at most "
                f"{JUSTIFICATIONS_MAX}, each reason 1-{REASON_MAX} characters)",
                "Pass {violation_id: reason} for the new ERC errors you leave open.",
            )
        return {k: v.strip() for k, v in raw.items()}

    async def step_done(
        self, ctx: ToolContext, idx: Any, summary: Any, justifications: Any = None
    ) -> dict[str, Any]:
        state = self._turn(ctx)
        if not isinstance(idx, int) or isinstance(idx, bool):
            raise _invalid("Invalid step", "step must be an integer", "Pass the step number.")
        summary = _text(summary, "summary", SUMMARY_MAX)
        given = self._justifications(justifications)
        pid = ctx.project.id
        async with self.lock(pid):
            plan = await self._open(pid)
            step = plan.step(idx)
            if plan.status != "running" or step.status != "running":
                raise PlanStateError(
                    f"Plan step {idx} cannot be finished",
                    f"The plan is {plan.status} and step {idx} is {step.status}",
                    "Call plan_step_begin for the step first.",
                )
            if state.active_step_id != step.id:
                raise PlanStateError(
                    f"Plan step {idx} is not active in this turn",
                    "A step begins and finishes in the same turn",
                    "Ask the user to resume the plan.",
                )
            delta, baseline_id = await self._erc(ctx, plan, step, state)
            new_ids = {str(v.get("id")) for v in delta.new_errors}
            open_errors = [v for v in delta.new_errors if str(v.get("id")) not in given]
            justified = {k: v for k, v in given.items() if k in new_ids}
            if open_errors:
                return await self._refuse_erc(
                    ctx, plan, step, state, delta, baseline_id, justified, open_errors
                )
            if self.history is None:
                raise KicadUnavailable("no history service: the checkpoint cannot be taken")
            rid = await self.editing.erc_results.add(
                pid, str(ctx.run_id), step.id, baseline_id, delta, justified
            )
            snap = await take_step_checkpoint(
                self.history, ctx.project, str(ctx.run_id), step.id, f"Plan step {idx}"
            )
            count = await self.repo.step_change_count(pid, str(ctx.run_id), step.id)
            new = machine.transition(
                plan,
                "step_done",
                now=utcnow_iso(),
                idx=idx,
                checkpoint_id=snap.id,
                change_count=count,
                summary=summary,
                erc_result_id=rid,
            )
            await self._save(new)
        notes = await self._step_notes(ctx, state, given, new_ids)
        self.clear_step(state)
        nxt = new.next_step
        result: dict[str, Any] = {
            "step": idx,
            "status": "done",
            "plan_status": new.status,
            "checkpoint_id": snap.id,
            "change_count": count,
            "erc": {
                "new_errors": len(delta.new_errors),
                "justified": sorted(justified),
                "new_warnings": [_brief(v) for v in delta.new_warnings[:ERRORS_LISTED]],
                "resolved": len(delta.resolved),
            },
            "next_step": None if nxt is None else nxt.idx,
            "notes": notes,
        }
        return result

    async def _step_notes(
        self, ctx: ToolContext, state: EditTurnState, given: dict[str, str], new_ids: set[str]
    ) -> list[str]:
        notes: list[str] = []
        unknown = sorted(set(given) - new_ids)
        if unknown:
            notes.append(
                "These justification ids match no new ERC error and were ignored: "
                + ", ".join(unknown[:10])
            )
        settings = await self.editing.settings_repo.get(ctx.project.id)
        rendered = (
            state.last_render_seq is not None
            and state.step_begin_seq is not None
            and state.last_render_seq > state.step_begin_seq
        )
        if settings.show_agent_view and not rendered:
            notes.append(
                "No render of the schematic since this step began: call render_view(doc='sch', "
                "region='around <refs>') and check the step's region before the next step."
            )
        return notes

    async def _refuse_erc(
        self,
        ctx: ToolContext,
        plan: Plan,
        step: PlanStep,
        state: EditTurnState,
        delta: erc.ErcDelta,
        baseline_id: str | None,
        justified: dict[str, str],
        open_errors: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Spec §6.3 / §11: refused while new errors are neither fixed nor justified; the
        second identical refusal of the same step pauses the plan (needs attention)."""
        ids = frozenset(str(v.get("id")) for v in open_errors)
        listed = [_brief(v) for v in open_errors[:ERRORS_LISTED]]
        details: dict[str, Any] = {"errors": listed, "count": len(open_errors)}
        if state.erc_refusal != (step.id, ids):
            state.erc_refusal = (step.id, ids)
            raise StructuredToolError(
                f"Plan step {step.idx} has {len(open_errors)} new ERC error(s) left open",
                "Every new ERC error of a step must be fixed or justified before it is done",
                "Fix each listed error, or pass erc_justifications {violation_id: reason} for "
                "the ones that are intended (e.g. 'connector follows in step 4'), then call "
                "plan_step_done again.",
                code="erc_errors_open",
                details=details,
            )
        pid = ctx.project.id
        rid = await self.editing.erc_results.add(
            pid, str(ctx.run_id), step.id, baseline_id, delta, justified
        )
        names = ", ".join(f"{v['type']} ({v['id']})" for v in listed[:5])
        new = machine.transition(
            plan,
            "fail",
            now=utcnow_iso(),
            reason="erc_regression",
            failure=f"{len(open_errors)} new ERC error(s) neither fixed nor justified: {names}",
        )
        new = machine.with_step(new, step.idx, erc_result_id=rid)
        await self._save(new)
        self.clear_step(state)
        engine = self._engine(pid)
        if engine is not None:
            await engine.post_error(
                f"Plan paused at step {step.idx}: new ERC errors were neither fixed nor "
                "justified. Review the step and resume, roll back or abort the plan.",
                {"code": "plan_needs_attention", "planId": plan.id, "ercResultId": rid},
            )
        details["plan_paused"] = True
        raise StructuredToolError(
            f"Plan paused: step {step.idx} still has unexplained new ERC errors",
            "The same ERC errors were left open twice",
            "Stop editing and explain the errors to the user; they resume, roll back or "
            "abort the plan. Revise it with plan_revise if the steps must change.",
            code="erc_errors_open",
            details=details,
        )

    async def fail_step(self, ctx: ToolContext, reason: str, failure: str) -> Plan | None:
        """Pause the running plan with its active step `failed` (PlanGate: step budget
        exceeded)."""
        pid = ctx.project.id
        async with self.lock(pid):
            plan = await self.repo.open_for(pid)
            if plan is None or plan.status != "running":
                return None
            new = machine.transition(plan, "fail", now=utcnow_iso(), reason=reason, failure=failure)
            await self._save(new)
        if ctx.run_id is not None:
            self.clear_step(self.editing.turn(pid, ctx.run_id))
        engine = self._engine(pid)
        if engine is not None:  # spec §7.2: a failed step pushes "needs attention"
            idx = plan.running_step.idx if plan.running_step is not None else "?"
            await engine.post_error(
                f"Plan paused at step {idx}: {failure}. Review it and resume, roll back or "
                "abort the plan.",
                {"code": "plan_needs_attention", "planId": plan.id, "reason": reason},
            )
        return new

    # ---- pauses (turn end, startup, external change)
    async def pause(self, project: Project, reason: str, step_status: str | None = None) -> None:
        """Pause the project's running plan; its running step becomes `step_status`
        (`failed` keeps a failure text, anything else = interrupted)."""
        async with self.lock(project.id):
            plan = await self.repo.open_for(project.id)
            if plan is None or plan.status != "running":
                return
            now = utcnow_iso()
            if step_status == "failed":
                new = machine.transition(
                    plan, "fail", now=now, reason=reason, failure=reason.replace("_", " ")
                )
            elif reason == "turn_ended":
                new = machine.transition(plan, "turn_end", now=now)
            else:
                new = machine.transition(plan, "interrupt", now=now, reason=reason)
            await self._save(new)

    async def on_turn_end(
        self, project: Project, run_id: str, outcome: TurnOutcome, engine: Any
    ) -> None:
        """Spec §7.2: a turn never leaves a plan running (Stop -> interrupted, KiCad
        failure -> step failed, otherwise turn_ended)."""
        st = await self.repo.open_status(project.id)
        if st is None or st[1] != "running":
            return
        if outcome.kicad_failed:
            reason, step_status = "kicad_failed", "failed"
        elif outcome.document_inconsistent:
            reason, step_status = "document_inconsistent", "failed"
        elif outcome.reason == "interrupted":
            reason, step_status = "interrupted", "interrupted"
        elif outcome.reason == "rate_limited":
            reason, step_status = "rate_limited", "interrupted"
        else:
            reason, step_status = "turn_ended", "interrupted"
        await self.pause(project, reason, step_status)
        if engine is not None:
            await engine.post_notice(
                f"The plan is paused ({reason.replace('_', ' ')}). Resume it to continue, "
                "roll back to a finished step or abort it.",
                {"action": "plan-paused", "planId": st[0], "reason": reason},
            )

    async def pause_all_running(self) -> None:
        """Startup: a server restart leaves no plan running (its step interrupted)."""
        for plan in await self.repo.running():
            async with self.lock(plan.project_id):
                current = await self.repo.get(plan.project_id, plan.id)
                if current.status != "running":
                    continue
                new = machine.transition(
                    current, "interrupt", now=utcnow_iso(), reason="server_restart"
                )
                await self._save(new)

    async def on_external_change(self, project: Project, files: list[str]) -> None:
        """History external-change listener (spec §5.3): the plan's base changed."""
        async with self.lock(project.id):
            plan = await self.repo.open_for(project.id)
            if plan is None or plan.status not in ("approved", "running", "paused"):
                return
            new = machine.transition(plan, "external_change", now=utcnow_iso())
            await self._save(new)
        await self._notice(
            project.id,
            "The plan is paused: its base changed because design files were changed outside "
            "CopperChat. Resume, roll back or abort it.",
            {"action": "plan-paused", "planId": plan.id, "reason": "external_change"},
        )
        self._note(
            project.id,
            f"Plan {plan.id} is paused because the design files changed outside CopperChat; "
            "the state it was planned on is gone. Do not continue it until the user resumes it.",
        )

    # ---- REST events (E-C12)
    async def _submit(self, project: Project, plan: Plan, event: str, text: str) -> None:
        engine = await self.runtime.ensure(project)
        await engine.submit_ex(
            f"plan-{plan.id}-{event}-{plan.revision}-{plan.event_seq}",
            text,
            [],
            {"planId": plan.id, "event": event},
        )

    async def _enqueue_event(self, project: Project, plan_id: str, event: str) -> Plan:
        async with self.lock(project.id):
            plan = await self.repo.get(project.id, plan_id)
            new = machine.transition(plan, event, now=utcnow_iso())
            await self.repo.save(new)
            if event == "approve":
                text = (
                    f"The user approved plan {new.id} (revision {new.revision}). "
                    "Execute it step by step."
                )
            else:
                nxt = new.next_step
                at = nxt.idx if nxt is not None else len(new.steps)
                text = f"The user resumed plan {new.id} at step {at}."
            try:
                await self._submit(project, new, event, text)
            except BaseException:
                # No state change; keep the event_seq bump so a retry never reuses the
                # client_message_id of a message that may have been stored.
                await self.repo.save(replace(plan, event_seq=new.event_seq))
                raise
        await self._publish(new)
        return new

    async def approve(self, project: Project, plan_id: str) -> Plan:
        return await self._enqueue_event(project, plan_id, "approve")

    async def resume(self, project: Project, plan_id: str) -> Plan:
        return await self._enqueue_event(project, plan_id, "resume")

    async def _close(self, project: Project, plan_id: str, event: str) -> Plan:
        async with self.lock(project.id):
            plan = await self.repo.get(project.id, plan_id)
            new = machine.transition(plan, event, now=utcnow_iso())
            await self._save(new)
        word = "rejected" if event == "reject" else "aborted"
        kept = "" if event == "reject" else " The design is kept as it is."
        engine = await self.runtime.ensure(project)
        await engine.post_notice(
            f"The user {word} the plan.{kept}", {"action": f"plan-{word}", "planId": new.id}
        )
        engine.add_agent_note(
            f"The user {word} plan {new.id}; it is closed.{kept} Ask the user how to "
            "continue before proposing another plan."
        )
        return new

    async def reject(self, project: Project, plan_id: str) -> Plan:
        return await self._close(project, plan_id, "reject")

    async def abort(self, project: Project, plan_id: str) -> Plan:
        return await self._close(project, plan_id, "abort")

    async def rollback(self, project: Project, plan_id: str, to_step: Any) -> Plan:
        """Restore step `to_step`'s checkpoint (0: the pre-plan snapshot); later steps
        become pending. Refused with `agent_busy` (409) while a run is active."""
        from copperchat.history.restore import AgentBusy, restore_snapshot

        async with self.lock(project.id):
            plan = await self.repo.get(project.id, plan_id)
            if (
                not isinstance(to_step, int)
                or isinstance(to_step, bool)
                or not (0 <= to_step <= len(plan.steps))
            ):
                raise InvalidPlanArgument(
                    "Invalid rollback target",
                    f"toStep must be an integer 0-{len(plan.steps)}",
                    "Pick a finished step or 0.",
                )
            engine = self._engine(project.id)
            if engine is not None and engine.is_busy:
                raise AgentBusy("the agent is working; stop it or wait before rolling back")
            new = machine.transition(plan, "rollback", now=utcnow_iso(), to_step=to_step)
            target = plan.base_snapshot_id if to_step == 0 else plan.step(to_step).checkpoint_id
            if target is None and plan.done_count > to_step:
                raise PlanStateError(
                    "Cannot roll back to the start",
                    "The plan has no snapshot from before its first step",
                    "Abort the plan, then restore a snapshot from the History panel.",
                )
            if target is not None:
                if self.history is None:
                    raise CopperChatError("history is not available")
                await restore_snapshot(self.history, project, target)
            await self._save(new)
        return new

    async def get(self, project: Project, plan_id: str) -> Plan:
        return await self.repo.get(project.id, plan_id)

    async def open_plan(self, project: Project) -> Plan | None:
        return await self.repo.open_for(project.id)
