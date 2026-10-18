# Phase E10 — Editing skill, scenario tests, live eval, docs

> Index: all contracts. Spec §9 (skills, part notes, research rule), §12 (scenario,
> undo, round-trip, live eval). Spike 2D (top skill rules, draft skill
> `spikes/2d-agent-eval/skill/v2/kicad-schematic-editing/SKILL.md`, `scenarios.py` role-based golden checkers,
> `eval.py`, `checks.py` layout checks).

**Goal:** the agent knows how to edit well (skill), and CI proves the tools build
the three reference circuits from an empty project with golden connectivity, clean
ERC, undo and loadable files; an opt-in live eval tracks the real model.

**Files:**
- Create: `backend/src/copperchat/skills/builtin/kicad-schematic-editing/SKILL.md`
- Modify: `skills/builtin/kicad-schematic-style/SKILL.md` (placement recipes),
  `skills/builtin/copperchat-tools/SKILL.md` (editing tools, `parts_get_notes`, plans)
- Create: `backend/tests/kicad/edit/test_scenarios.py`, `tests/kicad/edit/golden.py`
  (role-based checker, port of 2D `scenarios.py`), `tests/kicad/edit/test_undo.py`,
  `backend/tests/live/test_editing_eval.py` (+ `tests/live/editing_scenarios.py`)
- Create: `scripts/make-edited-fixtures.py`, `frontend/e2e/fixtures/edited/…` (KiCad-written)
- Modify: `frontend/e2e/viewer.spec.ts`, `docs/architecture.md`, `README.md`, `CHANGELOG.md`

---

### Task 1: Skills

- [x] **Step 1: Failing tests** (`tests/unit/phase8/test_builtin_skills.py` or the existing skill
  test module): `kicad-schematic-editing` exists with valid front matter and the sections
  "Tool order", "Addressing pins", "Rails and labels", "Plans", "Self-check", "Missing parts";
  every tool name mentioned in the three skills exists in `registry.all_specs()`
  (`test_skill_tool_names_exist`); `kicad-schematic-style` contains the recipes listed in spec §9.
- [x] **Step 2:** FAIL. **Step 3: Write** the skill from 2D's v2 draft and its seven top rules
  (index of 2D Decision), plus: research before active parts (`parts_get_notes` →
  `research_part`), budget/plan etiquette (step size 5–10 logical changes, never exceed a plan),
  ERC policy (fix or justify errors one by one), render + checklist at each step end, missing
  library part → closest matches + ask the user, never invent; an `unverified` footprint
  (project without a board, owner decision 3) is reported to the user. **Step 4:** PASS. **Step 5:** commit.

### Task 2: Golden scenario tests (CI, real KiCad, tools only)

**Interfaces:** `golden.check(scenario: str, netlist: Netlist, components: dict) -> dict`
(`{"golden": bool, "missing": [...]}`); scenarios `s1_led`, `s2_ldo`, `s3_mcu_i2c_subsheet`.

- [x] **Step 1: Failing kicad tests** (`blank_session`, tool handlers called through the
  adapter with `edit_ctx`, scripted like 2D `scripted.py`):
  `test_s1_led_resistor`, `test_s2_ldo_with_caps_0805`, `test_s3_mcu_decoupling_i2c_sensor_on_subsheet`
  (sensor on a sheet made by `sch_add_sheet`, I2C through sheet pins). Each asserts: golden
  connectivity by role (live netlist **and** `kicad-cli sch export netlist`), `run_erc` 0 errors,
  every item on grid, no symbol box overlap, no wire interior over a pin end, footprints 0805
  for passives; and that the change log equals the `kicad-cli sch diff` against the start
  snapshot (UUID + op; side effects flagged).
- [x] **Step 2:** FAIL until all tools exist (they do after E4–E8b). **Step 3:** fix gaps found
  (bugs go into the owning module with a regression test). **Step 4:** PASS. **Step 5:** commit.

### Task 3: Undo and round-trip

- [x] **Step 1: Failing tests**: `test_undo.py` — run s2 as a two-step plan through `PlanService`;
  after each step record file hashes; restoring each step checkpoint and the turn snapshot
  yields byte-identical files; `test_rollback_half_executed_plan` (pause after step 1 → rollback
  to 0 → files equal the base snapshot). `scripts/make-edited-fixtures.py` (kicad image) saves the
  three scenario results into `frontend/e2e/fixtures/edited/`; `viewer.spec.ts` loads each and
  asserts no page/console error and ≥ 1 symbol rendered.
- [x] **Step 2:** FAIL. **Step 3: Implement.** **Step 4:** PASS. **Step 5:** commit.

### Task 4: Live eval (opt-in) and docs

- [x] **Step 1:** `tests/live/test_editing_eval.py` (marker `live_sdk`, skipped without
  `CLAUDE_CODE_OAUTH_TOKEN`): runs the three 2D prompts plus "change all 0603 resistors to
  0402" through the real backend (`SdkStreamingBackend`, real tools), scores golden, ERC errors,
  tool calls, tokens, cost; writes `backend/tests/live/results/<date>.json`. Not CI-gating.
- [x] **Step 2:** docs: `docs/architecture.md` editing section (two-phase tools, verification,
  plans, change log, diff), README feature list, `CHANGELOG.md` "Unreleased".
- [x] **Step 3:** full suites green; commit `test: editing scenarios, undo, live eval; docs`.

---

**Done when:** one PR, CI green, review-loop done; one live eval run recorded in the PR body.
