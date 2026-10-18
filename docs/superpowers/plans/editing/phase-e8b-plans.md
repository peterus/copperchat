# Phase E8b — Plans: model, gate, tools, REST

> Index contracts: E-C3, E-C5, E-C6, E-C10, E-C11, E-C12. Spec §6.3, §7 (all),
> §5.3 (pause on crash/external change), §8.3 (ERC policy), §11. Owner decisions:
> no edits outside an open plan; budget 20 logical changes (index).

**Goal:** larger changes run as approved plans, step by step, each step checked
(ERC errors fixed or justified) and checkpointed; the server enforces budget and
plan discipline.

**Files:**
- Create: `backend/src/copperchat/plans/{__init__,model,machine,repo,service,gate,specs}.py`,
  `web/routes_plans.py`, `protocol-fixtures/plan-updated.json`
- Modify: `agent/protocol.py` (`PlanUpdatedFrame`), `frontend/src/protocol/frames.ts`,
  `editing/service.py` (`gate = PlanGate`), `editing/hooks.py` (turn end/start),
  `history/hooks.py` / `history/service.py` (external-change listener), `web/app.py`
  (router, `PlanService` in lifespan + `pause_all_running()`), `tools/registry.py`
  (provider `copperchat.plans.specs`)
- Tests: `tests/unit/plans/test_machine.py`, `test_gate.py`, `test_service.py`,
  `tests/unit/web/test_routes_plans.py`, `tests/kicad/edit/test_plan_flow.py`

---

### Task 1: Model and state machine

**Interfaces:** Produces `Plan`, `PlanStep`, `PlanStatus`, `OPEN`, `machine.transition(plan,
event, **kw) -> Plan` (pure; raises `PlanStateError(code="plan_state")`), `PlanRepo`, `PlanDTO`.

- [ ] **Step 1: Failing unit tests** (`test_machine.py`): one test per row of spec §7.2, e.g.
  `test_propose_creates_revision_1`, `test_reject_only_from_proposed`,
  `test_revise_from_paused_returns_to_proposed`, `test_approve_then_step_begin_runs`,
  `test_last_step_done_finishes`, `test_interrupt_pauses_with_step_interrupted`,
  `test_turn_end_with_steps_left_pauses_turn_ended`, `test_resume_returns_to_approved`,
  `test_rollback_marks_later_steps_pending`, `test_abort_terminal`,
  `test_invalid_transition_raises`; repo: `test_second_open_plan_rejected` (`ux_plans_open`).
- [ ] **Step 2:** FAIL. **Step 3: Implement.** **Step 4:** PASS. **Step 5:** commit.

### Task 2: `PlanGate` (policy, budget, open plan, step budget)

**Interfaces:** `PlanGate(BudgetGate)` implementing E-C5/E-C6 plus spec §7.1.

- [ ] **Step 1: Failing unit tests** (`test_gate.py`, adapter + fake specs as in E2):
  `test_plan_open_refuses_mutation` (`proposed`/`approved`/`paused` → `plan_open`, no prepare,
  no snapshot; read-only tools still work); `test_running_outside_step_refused`
  (`plan_step_required`); `test_inside_step_counts_against_expected_not_turn_budget`;
  `test_step_tolerance_50_percent` (expected 10: 15 ok, 16 → plan paused, step `failed` with
  reason, error `plan_step_exceeded`); `test_turn_budget_refusal_suggests_plan_propose`
  (`plan_required`, budget 20); `test_policy_always_without_step_refused`.
- [ ] **Step 2:** FAIL. **Step 3: Implement.** **Step 4:** PASS. **Step 5:** commit.

### Task 3: Plan tools and service

**Interfaces:** tools `plan_propose(title, summary, steps: [{title, intent, expected_changes:
int, touches?: {refs, nets}, verification}])`, `plan_revise(steps, reason)`,
`plan_step_begin(step: int)`, `plan_step_done(step: int, summary, erc_justifications?: {violation_id: reason})`;
`PlanService` (E-C11). `plan_propose` posts a notice `{action: "plan", planId}`.

- [ ] **Step 1: Failing tests** (`test_service.py`, fake history/ERC; `test_plan_flow.py` real KiCad):
  - `test_propose_refused_while_open` (suggests `plan_revise`); `test_revise_only_proposed_or_paused`.
  - `test_step_begin_1_takes_base_snapshot` (`on_mutation` called once; `base_snapshot_id` set).
  - `test_step_done_refused_with_unjustified_error` (`erc_errors_open` with the list; step
    stays `running`); `test_step_done_accepts_justification` (stored on the step, visible in DTO);
    `test_step_done_after_fix_accepts`.
  - `test_step_done_takes_checkpoint_and_counts_changes` (checkpoint id + `change_count` from the
    change log of the step).
  - `test_step_done_without_render_adds_reminder` (no `render_view(sch)` since step begin →
    result contains the reminder line; no reminder when the project's `show_agent_view` setting is off).
  - `test_turn_end_pauses_running_plan`; `test_kicad_failed_marks_step_failed`;
    `test_external_change_pauses_plan` (history external-change listener; notice says the plan's
    base changed); `test_startup_pauses_running` (`pause_all_running`).
  - kicad `test_two_step_plan_end_to_end` (`blank_session`: propose 2 steps → approve (service)
    → step 1 places R + LED via E4 tools → done (checkpoint) → step 2 connects → done → plan
    `done`; restoring step 1's checkpoint gives byte-identical files to those recorded at step 1).
- [ ] **Step 2:** FAIL. **Step 3: Implement** (ERC via E8a `current_erc` + `erc_baseline` of the
  previous step checkpoint, or of `base_snapshot_id` for step 1; checkpoint via
  `take_step_checkpoint`). **Step 4:** PASS. **Step 5:** commit `feat: plan tools`.

### Task 4: REST, WS and rollback

**Interfaces:** E-C12 plan routes; `plan-updated` frame on every plan change; approve/resume
enqueue a user message through `engine.submit_ex(client_message_id=f"plan-{id}-{event}-{revision}", text=…)`
("Plan "<title>" approved. Execute it step by step." / "Resume plan "<title>" at step k.").

- [ ] **Step 1: Failing tests** (`test_routes_plans.py`): `test_approve_enqueues_message_once`
  (idempotent on retry); `test_reject_posts_notice_for_agent`; `test_resume_requires_paused`;
  `test_rollback_restores_checkpoint_and_resets_steps`; `test_rollback_to_zero_restores_base`;
  `test_rollback_refused_while_run_active` (409 `agent_busy`); `test_abort_keeps_design`;
  `test_open_plan_endpoint`; 401/404/400 cases; `plan-updated` fixture valid/invalid
  (backend + `frames.test.ts`).
- [ ] **Step 2:** FAIL. **Step 3: Implement** (rollback uses `restore_snapshot`). **Step 4:** PASS.
  **Step 5:** commit `feat: plan REST and plan-updated frames`.

---

**Done when:** one PR, CI green, review-loop done, E-C11 as built.
