# Round 2 — Phase R7: OpenDriveHub acceptance run of round 2

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Read first: the index (incl. "Round 2" and every "As built", especially P9a and R1–R6), spec
> §18.3, §19 and **§22.6**, `phase-p9a-golden-odh.md`. **Wave R-4: starts when R1–R6 are merged.**

**Goal:** repeat the ODH dry run with every round-2 change and report honestly whether the
agent's tools now lay out the OpenDriveHub transmitter main board (76 parts) to the owner's
bar: 80 × 60 mm if physically feasible, else 80 × 100 mm well spread; critical routing without
pad-width `no_path`; the fill passing the 20 % rule; the final full check's hard gates incl.
M15/M16; memory against ≈ 5 GB. Tool defects found are fixed here (minimal, in the owning
files), thresholds never.

**Architecture:** `scripts/odh-dry-run.py` drives `tests/kicad_e2e/flow.run_flow` with an ODH
answers JSON outside the repo (never committed): sizes tried in order 80 × 60 then 80 × 100;
blocks from `pcb_analyze` + `pcb_place_blocks`; the scripted ladder as in P9a. The report goes
into the PR body (numbers, renders described, failures with causes).

`cc/` = `backend/src/copperchat/`.

## Global Constraints

- Index Global Constraints + "Round-2 phase conventions". No OpenDriveHub data, names or
  answers in the repo (the script stays generic; answers live outside).
- The ODH copy is made by the script (`copy_checkout`, original never written).

## Review Focus (this phase)

1. A fix-forward that changes a threshold or a golden without explanation → refused in review;
   every golden change explained.
2. The 80 × 60 attempt failing for a placer reason while `courtyard_fill` says it is feasible
   → reported as a placer defect (with numbers), not as "too small".

---

### Task 1: Script — size ladder and spread/fill reporting

**Files:** Modify `scripts/odh-dry-run.py` (`--sizes 80x60,80x100` default from the answers'
`outline`, one run per size until one reaches a fab package or all fail; report per size:
`courtyard_fill`, `spread`, the placement score, per-step seconds, the fill report, the final
metrics, memory incl. R6's concurrent peak); Test: `backend/tests/unit/test_odh_dry_run.py`
(existing; append tests for the size parser and the per-size report section).

- [ ] **Step 1: Failing tests** for the size parser (`"80x60,80x100"` → two outlines; invalid →
  argparse error) and the per-size report section. **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(scripts): odh dry run size ladder`.

### Task 2: The acceptance run

- [ ] **Step 1:** run (kicad-base + Freerouting, `timeout 5400`) on a fresh copy of the ODH
  checkout the coordinator names (P9a used `OpenDriveHub@2e17ac5` + the PROD working tree); keep
  `report.md`, `metrics.json` and renders under `/tmp/sp4-renders/r7-*`.
- [ ] **Step 2:** every hard-gate failure: find the cause; a tool defect → fix in the owning
  file with a unit test (TDD) in this PR, re-run; a design-level limit (e.g. the board really
  is too small) → reported with numbers. Up to three fix-and-rerun cycles; then report what is
  left.
- [ ] **Step 3:** `uv run pytest -m kicad_e2e -v` stays green after any fix; golden regenerated
  only with an explanation.

### Task 3: Report, index, review

- [ ] "As built (R7)": size reached, `courtyard_fill` at 80 × 60, `spread`, critical routing
  (seconds, failures), fill (seconds, ratio, rungs), final metrics table M1–M16 with verdicts,
  remaining failures with cause, memory (concurrent peak and parts), fixes made, renders' paths.
  Update the index "Residuals" with what stays open for P9b / later.
- [ ] CHANGELOG; lint/type; PR `board round 2 R7: OpenDriveHub acceptance run`; CI green;
  review-loop; done report to the coordinator: pass/fail per acceptance item of spec §22.6, the
  renders (paths) for the owner, and every decision only the owner can make.
