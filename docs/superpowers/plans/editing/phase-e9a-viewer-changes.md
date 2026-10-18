# Phase E9a — Viewer highlight, Changes panel, diff layer

> Index contracts: E-C12, E-C13. Spike 2C (fork patch
> `spikes/2c-diff-highlight/patches/schematic-highlight-set.patch`, cross-sheet
> behaviour, follow-ups 1, 2, 6). Frontend conventions: foundation C7a (Phase 6A/6B
> as built). KiCanvas bump procedure: `frontend/vendor-patches/kicanvas/README.md`.

**Goal:** after an editing turn the user sees what changed: a per-turn/step change
list, changed items highlighted in the viewer (also on sub-sheets), an optional
diff layer and a before/after toggle.

**Files:**
- Fork `peterus/kicanvas`, branch `copperchat` (never force-push): commit
  "schematic highlight overlay" — productionised 2C patch.
- Modify: `frontend/public/vendor/kicanvas/{KICANVAS_COMMIT,kicanvas.js,BUILD_INFO}`,
  `frontend/vendor-patches/kicanvas/README.md` (new row: CopperChat-specific),
  `frontend/src/lib/kicanvas.ts`, `components/viewer/{types.ts,KicanvasPane.tsx,Viewer.tsx,KicadDocView.tsx}`,
  `stores/view.ts`, `panels/index.ts`
- Create: `frontend/src/stores/changes.ts`, `frontend/src/panels/ChangesPanel.tsx`,
  `frontend/src/components/changes/{ChangeList,ErcBadge,BeforeAfter}.tsx`
- Tests: colocated `*.test.ts(x)`; Playwright `frontend/e2e/viewer.spec.ts` (highlight cases)

---

### Task 1: KiCanvas fork — highlight overlay

**Interfaces:** Produces the E-C13 viewer methods `highlight`, `highlight_boxes`,
`clear_highlight` (+ `highlighted_count`).

- [x] **Step 1: Failing Playwright tests** (`viewer.spec.ts`, real bundle on the ecc83 and
  complex_hierarchy fixtures): `highlight set of refs` (5 refs → `found` 5, unknown in
  `missing`, pixel count > 0); `clear restores identical screenshot`; `survives select`;
  `reset on page switch` (after `set_active_page` + `kicanvas:viewerload` the count is 0);
  `boxes in mm` (box from a diff entry of C105 within 0.5 mm of the symbol box);
  `colours per kind on both themes` (added green / modified orange / removed red outline,
  dark theme `witchhazel` readable — 2C follow-up 6).
- [x] **Step 2:** FAIL against the current bundle. **Step 3: Implement** in the fork: own overlay
  layer (not the selection's), theme-aware colours, reset in `load()`, targets by ref or UUID;
  bump `KICANVAS_COMMIT`, run `./scripts/vendor-kicanvas.sh && ./scripts/check-kicanvas.sh`,
  commit the bundle (CI `kicanvas-reproducible` must stay green).
- [x] **Step 4:** PASS. **Step 5:** commit `feat(viewer): KiCanvas highlight overlay`.

### Task 2: View store highlight + pane wiring

**Interfaces:** `@/stores/view` `highlight`, `setHighlight`, `clearHighlight`, `find`, `setFind`
(moved from `Viewer` local state); `KicadPaneProps.highlight?`; `KcViewer` optional methods.

- [x] **Step 1: Failing Vitest tests**: `view.test.ts` (`setHighlight` bumps `seq`; project switch
  clears it); `KicanvasPane.test.tsx` (fake viewer: highlight applied after load and re-applied
  on `kicanvas:viewerload`; for a `sheetPath` on another page the pane switches page first —
  same path as `runFind`; missing refs reported via `onFindResult`-like callback).
- [x] **Step 2:** FAIL. **Step 3: Implement** `runHighlight` next to `runFind`. **Step 4:** PASS.
  **Step 5:** commit.

### Task 3: Changes store and panel

**Interfaces:** `@/stores/changes` (`createChangesStore(ws)`, `load(projectId)`, `runs`,
`entries`, `loadEntries(runId, stepId?)`, refresh on `changes-updated` and after
`run-finished`); panel `changes` ("Changes", order 15).

- [x] **Step 1: Failing Vitest tests**: `changes.test.ts` (frame → refetch runs once, debounced);
  `ChangesPanel.test.tsx`: lists runs newest first with count and ERC badge (`errors` red,
  `warnings` amber, justified errors shown with their reason); entries grouped by step; side
  effects marked "side effect"; tapping an entry calls `setHighlight({refs:[ref]|[], boxes:
  …, sheetPath})` and focuses the viewer tab on phones; "Show all changes" highlights every
  entry of the run; "Diff" toggle fetches `/changes/diff?snapshotId=` and sets boxes by kind;
  "Before / after" toggle shows the snapshot preview (`previewUrl`) against the current agent
  view.
- [x] **Step 2:** FAIL. **Step 3: Implement** (paths without `/api`, X10; strict zod parsing of
  REST payloads). **Step 4:** PASS (+ `npm run typecheck && npm run lint && npm run build`).
  **Step 5:** commit `feat: Changes panel`.

---

**Done when:** one PR, CI green (frontend, kicanvas-reproducible, e2e), review-loop done.
Phone layout checked in Playwright `phone` project.
