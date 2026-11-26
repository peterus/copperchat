# Round 2 — Phase R4: fill tuning, Y → T post-processing, M15/M16 hard

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Read first: the index (incl. "Round 2", "As built (P6a)", "As built (P6b)", "As built (P9a)"
> → ODH dry run), spec §10.3, §10.4 and **§22.4 / §22.5**, phase files P6a/P6b. Index
> contracts: B-C12 post-processor + B-C13 round-2 blocks. **Wave R-3: starts when R2 and R3 are
> merged.**

**Goal:** on ODH (R2 placement, R3 critical routing) the fill passes the **unchanged** 20 %
rule within `timeout_s` ≤ 600 s; Freerouting's Y-junctions become T-junctions or are ripped
(`y_junction`); T-junctions are no longer ripped; neck-downs survive `_widen`; then M15 and
M16's hard parts are switched on and the golden pins them.

**Architecture:** four levers, each measured on ODH and recorded: (1) **fewer fill nets** —
intra-block nets join the critical set (`classify._critical`), so Freerouting routes only
inter-block nets through R2's channels; (2) **Freerouting settings** — only options the pinned
2.5.0 accepts (probed; P6b found that unknown properties are rejected and
`--router.automatic_neckdown=false` had no effect); (3) **optimizer** — P6b found Freerouting
reads locked pad-to-via links as unrouted, so its optimizer ("only on completely routed boards")
never runs; find the cause (DSN pad/via geometry or resolution) and fix it on our side; (4)
**post-processing** — Y → T re-landing in `chamfer.py`.

`cc/` = `backend/src/copperchat/`.

## Global Constraints

- Index Global Constraints: `RIP_RATIO_MAX = 0.20` unchanged (spec §10.4); `FILL_WORST_CASE_S`
  1005 s unchanged; DSN text edits only as owner D4 last resort, each recorded; Freerouting
  stays sandboxed (no network, `prlimit`), version 2.5.0 pinned.
- R6's heavy gate wraps every Freerouting run; R1's `geom` is the only junction definition.

## Review Focus (this phase)

1. Y re-landing that creates a 90° vertex on the branch (previous turn becomes 90°) → not
   applied; ripped instead. → `test_y_to_t_rejects_ninety_on_branch`.
2. Re-landing point outside the through-track's segment (projection beyond its end) → rip.
   → `test_y_to_t_projection_outside`.
3. A probed Freerouting option that is accepted but silently ignored → not kept (each kept
   option needs a measured effect). → Task 2 record.
4. `_widen` widening a neck inside the escape zone into a DRC violation → kept narrow.
   → `test_widen_keeps_neck`.

---

### Task 1: Intra-block nets in the critical set

**Files:** Modify `cc/board/classify.py` (`_critical` gains the blocks: a non-ground net whose
pads all belong to one block's refs, ≤ `CRITICAL_INTRA_MAX_PADS = 8` pads); Test:
`backend/tests/unit/board/test_classify.py`, `backend/tests/kicad/board/goldens/sp4_synth_classification.json`
(regenerate, explain).

- [ ] **Step 1: Failing tests:** `test_critical_includes_intra_block_net`,
  `test_critical_excludes_inter_block_net`, `test_critical_excludes_ground`.
- [ ] **Step 2–4:** FAIL → implement → PASS; `UPDATE_GOLDENS=1` for the classification golden
  after rebase. **Step 5: Commit** `feat(board): intra-block nets join the critical set`.

### Task 2: Freerouting settings probe and tuning

**Files:** Modify `cc/board/freerouting.py` (`FREEROUTING_SETTINGS: tuple[str, ...]`, passed
by the fill as `extra_args`), `cc/kicad_tools/board/fill.py` (pass them; `PASSES_DEFAULT` if
re-tuned); Test: `backend/tests/kicad/board/test_fill_roundtrip.py` (probes, `record_property`),
`backend/tests/unit/board/test_freerouting_unit.py`.

- [ ] **Step 1: Probe** (kicad test, records only): the JAR's `--help` output and, for each
  candidate setting it lists that concerns passes, optimizer, via cost, ripup cost or thread
  count, whether `run_freerouting` accepts it (exit code, "Unknown settings property" in the
  log). Never assume a name; record the accepted list in "As built (R4)".
- [ ] **Step 2: Measure on ODH** (copy outside the repo, after R2/R3 + Task 1): passes
  {10, 20, 30} × each accepted setting that plausibly helps; per run record seconds per pass,
  completion, rip count by reason, ratio. Keep a setting only with a measured improvement of
  the ratio or the time at equal ratio.
- [ ] **Step 3: Optimizer cause** — compare the DSN `wiring` of locked pad-to-via links with a
  pad's DSN geometry (via at the pad centre vs. off-pad, the DSN `resolution`); a test that
  Freerouting's log reports 0 unrouted for a fully own-routed `sp4_synth` subset after the fix;
  if the fix needs DSN text editing, apply only per D4 and record.
- [ ] **Step 4:** unit test pins `FREEROUTING_SETTINGS` reach the command line
  (`test_settings_in_command`). **Step 5: Commit** `feat(board): Freerouting settings tuned on dense boards`.

### Task 3: Post-processor Y → T; T kept; `_widen` keeps necks

**Files:** Modify `cc/board/router/chamfer.py` (`postprocess`: stop ripping `t_junctions`;
re-land `y_junctions`), `cc/kicad_tools/board/fill.py` (`_widen` skips a track that satisfies
R1's M12 neck exemption; Y re-landing replacements DRC-checked like chamfers); Test:
`backend/tests/unit/board/test_chamfer.py`, `backend/tests/unit/board/test_fill_plan.py`,
`backend/tests/kicad/board/test_fill_postprocess.py`.

**Interfaces:** B-C12 post-processor round-2 block; new reason `y_junction`
(`PostResult.reasons`), counted in the fill report's `reasons`.

- [ ] **Step 1: Failing tests:** `test_t_junction_kept`, `test_y_to_t_relands`,
  `test_y_to_t_rejects_ninety_on_branch`, `test_y_to_t_projection_outside`,
  `test_y_unfixable_ripped_with_reason`, `test_widen_keeps_neck`, kicad: a fill on the routed
  rig → `analyse_tracks(model).y_junctions == ()` on the result.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(board): Freerouting Y-junctions to T or rip`.

### Task 4: ODH fill run

- [ ] **Step 1:** ODH dry run at the size R2 reached (80 × 60 if it fit, else 80 × 100), all
  levers on: record fill seconds, passes, completion, ratio, rips by reason, the ladder rungs
  used, M2 after the ladder, M4/M5/M7/M15/M16 on the filled board, renders
  `/tmp/sp4-renders/r4-*`. **Acceptance of this phase:** the fill passes the 20 % rule on ODH
  (possibly after ladder rung (c)) and no connection is left open. If not, report the exact
  remaining reasons and areas — do not relax a threshold.

### Task 5: Switch M15/M16 hard; golden

**Files:** Modify `cc/board/metrics.py` (`M15_HARD = True`, `M16_HARD = True`; R1's tests that
assume `False` switch to monkeypatching), golden.

- [ ] **Step 1: Failing test:** `test_golden_m15_m16_hard` (kicad_e2e: the golden flow's final
  full check has M15 = 0 and M16 hard = 0 with verdict `pass`).
- [ ] **Step 2:** switch; `uv run pytest -m kicad_e2e -v` — PASS (≤ 180 s); regenerate the
  golden after rebase; explain. **Step 3: Commit** `feat(board): M15/M16 are hard gates (owner rules a–c)`.

### Task 6: Index, CI, review

- [ ] "As built (R4)" (accepted settings and their measured effect, optimizer finding, ODH fill
  numbers), CHANGELOG; lint/type; PR `board round 2 R4: fill tuning and Y → T`; CI green;
  review-loop; done report with the ODH fill result.
