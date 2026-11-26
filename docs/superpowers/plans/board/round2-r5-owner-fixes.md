# Round 2 — Phase R5: owner fixes (preset holes, fab note, BOM/CPL, render extent)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Read first: the index (incl. "Round 2", "As built (P1c)", "As built (P8)", "As built (P9a)"),
> spec §14 and **§22.2 / §22.6**. **Wave R-1: no dependency.**

**Goal:** apply owner decision 2 (JLC presets' plated-hole minimum 0.2 mm + surcharge note),
keep mounting holes and excluded parts out of the JLC BOM/CPL (P8 residual), and make
`pcb_render`'s default view the board, not KiCad's "board area" (P9a finding: an item at
(7, 0.5) mm stretched the ODH view to three times the board).

**Architecture:** data change in `presets.py`; pure helpers in `cc/board/fab.py`
(`fab_excluded`, `small_holes`, `exclude=` on `bom_rows` / `cpl_rows`, two report sections);
pure `default_extent(model)` in `cc/board/render.py`, passed by `render_board` as the extent
when the caller gives no region.

`cc/` = `backend/src/copperchat/`.

## Global Constraints

- Index Global Constraints + "Round-2 phase conventions". `min_via_drill` stays 0.3 mm
  (round-2 plan call 5). M14 reads `min_drill` from the preset — no metric code change here.
- Fixtures stay KiCad-generated; the ODH probe runs on a copy outside the repo.

## Review Focus (this phase)

1. A grouped BOM row (`H1,H2,H3,H4`) must disappear entirely, a mixed row keep its other
   designators. → `test_bom_drops_excluded_designators`.
2. A board with no outline (sync just ran) → extent from courtyards only; nothing at all →
   KiCad's viewBox as today. → `test_default_extent_without_outline`.
3. An explicit `region` still wins over the default extent. → `test_region_overrides_extent`.

---

### Task 1: Presets `min_drill` 0.2

**Files:** Modify `cc/board/presets.py` (three presets, `min_drill = 0.2`),
`backend/tests/unit/board/test_presets.py` (`test_preset_values_call_8` values + a comment
"owner decision 2026-11-26"). Also check `pcb_set_rules` maps `min_drill → min_through_drill`
(`cc/kicad_tools/board/setup.py`) — unchanged code; `tests/kicad/board/test_rules.py` value
assertions updated if they pin 0.3.

- [ ] **Step 1:** update the test values → **Step 2:** run `uv run pytest tests/unit/board/test_presets.py -q` — FAIL
  → **Step 3:** change the data → **Step 4:** PASS → **Step 5:** commit `feat(board): JLC presets min hole 0.2 mm (owner)`.

### Task 2: BOM/CPL exclusion and the fab-report notes

**Files:** Modify `cc/board/fab.py` (`fab_excluded`, `small_holes`, `bom_rows(…, exclude=)`,
`cpl_rows(…, exclude=)`, `fab_report(…, excluded=)`), `cc/kicad_tools/board/fab.py` (compute
the classification once, pass `exclude` / `excluded`); Test: `backend/tests/unit/board/test_fab_plan.py`,
`backend/tests/kicad/board/test_fab_export.py`.

**Interfaces:** B-C1/B-C8/B-C9 (round 2) block. `fab_excluded` = refs classified
`mounting_hole` (classify.py `PartClass`) ∪ parts whose footprint attributes exclude them from
BOM / position files (read from `BoardModel` if the attribute is there; if `BoardModel` lacks
the attributes, add read-only fields `exclude_from_bom: bool = False`, `exclude_from_pos: bool =
False` to `Part`, not hashed, filled in `kicad_tools/board/read.py` — record in As built).
`small_holes`: every plated pad drill and via drill < 0.3 mm, sorted, refs deduplicated with the
smallest drill.

- [ ] **Step 1: Failing tests:** `test_bom_drops_excluded_designators` (row `H1,H2` → gone;
  `C1,H9` → `C1`), `test_cpl_drops_excluded`, `test_fab_excluded_mounting_holes`,
  `test_small_holes_lists_02` (a 0.2 mm pad drill → listed, 0.3 not), `test_report_sections`
  (report contains "Excluded from BOM/CPL" with the refs and "Small holes (possible JLC
  surcharge)" only when there are some; every string through `md`). Kicad:
  `test_fab_export_excludes_holes` on the P8 `fab_rig` (`sp4_synth` H1–H4 are mounting holes →
  absent from `bom-jlcpcb.csv` / `cpl-jlcpcb.csv`, listed in the report).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** unit + `uv run pytest -m kicad tests/kicad/board/test_fab_export.py` (kicad-base, `timeout 900`) — PASS.
- [ ] **Step 5: Commit** `fix(board): no mounting holes in JLC BOM/CPL; small-hole surcharge note`.

### Task 3: Render default extent

**Files:** Modify `cc/board/render.py` (`default_extent`), `cc/views/board_render.py`
(`render_board`: `extent = region or default_extent(model)` for every view except `3d`);
Test: `backend/tests/unit/board/test_render_wrap.py` (append), `backend/tests/kicad/board/test_render.py`.

**Interfaces:** `default_extent(model: BoardModel, markers: Sequence[Marker] = ()) -> Rect | None` —
outline bbox ∪ every footprint courtyard bbox (unplaced staging parts included, so the
placement view still shows them) ∪ marker points; `None` when all are empty. 3 % padding stays
in `wrap_svg`.

- [ ] **Step 1: Probe (record, do not commit data):** on an ODH copy (`scripts/odh-dry-run.py
  --odh <checkout> --project hardware/transmitter/pcb/main/main.kicad_pro --answers <json> --out
  <tmp>` stopped after placement, or a `pcb_status` + board read in a script) find the item(s)
  whose bbox reaches (7, 0.5) mm; record type and layer in "As built (R5)".
- [ ] **Step 2: Failing tests:** `test_default_extent_outline_and_courtyards`,
  `test_default_extent_without_outline`, `test_default_extent_none_when_empty`,
  `test_region_overrides_extent`; kicad: `test_render_extent_ignores_far_drawing` — on the
  placed `sp4_synth` rig add one text far outside through the engine (`BoardChangeSet`,
  board text item) and assert the PNG's wrapped viewBox ⊂ outline ∪ courtyards + padding.
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `fix(board): render extent from outline and courtyards`.

### Task 4: Index, CI, review

- [ ] "As built (R5)" (probe result, attribute fields if added, golden unchanged or regenerated
  with reasons), CHANGELOG line; lint/type; PR `board round 2 R5: preset holes, BOM/CPL, render
  extent`; CI green; review-loop; done report.
