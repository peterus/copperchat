# Phase F4b — `fp_approve`, `fp_delete`, local footprints in `sch_set_fields`

> Index contracts: FP-C7 (`footprint_reviews`), FP-C8 (`LibraryRowStage`, `run_library_row`,
> journal), FP-C9. Spec §7.7, §7.8, §7.9, §12, §13.1 (`FP_WRITES`), §16.1 (`fp_approve`
> guards, `COPPERCHAT_FOOTPRINT_TOOLS=off`, adapter row compensation), §16.2 (second
> project, row-stage hashes, same-turn use, `fp_approve`, `fp_delete` in use).
> Wave 4: starts when F4a is merged.

**Goal:** the user's chat approval is recorded only with server-side evidence that they saw
the current geometry; local footprints can be deleted safely; any project of the repo can
assign a local footprint, which adds its library row on the way.

**Files:**
- Create: `backend/src/copperchat/kicad_tools/footprints/{approve,delete,usage}.py`
- Modify: `db/repos/footprints.py` (+ `FootprintReviewRepo`), `web/ws.py` (submit with
  `metadata={"origin": "user"}`), `kicad_tools/footprints/{read,specs}.py` (record renders and
  checks; `fp_approve`, `fp_delete` specs), `kicad_tools/edit/specs_place.py`
  (`footprint_notes` local branch; `ChangeSet.library_row`), `agent/sdk_tools.py`
  (generic `library_row` step + compensation, inside the library lock), `agent/profiles.py`
  (`FP_WRITES` += `fp_approve`, `fp_delete`)
- Tests: `backend/tests/unit/footprints/{test_approve_guards,test_usage_scan,test_footprint_notes_local}.py`,
  `backend/tests/unit/agent/test_sdk_tools_library_row.py`, `backend/tests/unit/test_ws*.py`
  (origin stamp), `backend/tests/kicad/footprints/{test_fp_approve,test_fp_delete,test_assign_local}.py`

---

### Task 1: `fp_approve` and its guards

**Interfaces:** `FootprintReviewRepo.record(conversation_id, lib_id, geometry_hash, run_id,
*, rendered: bool = False, checked: bool = False, check_errors: int | None = None)` (upsert,
stamps `rendered_at`/`checked_at` with `utcnow_iso()`), `.get(conversation_id, lib_id,
geometry_hash) -> Row | None`. `fp_render` and `fp_check` call `record` when
`ctx.conversation_id` is set (local and stock footprints alike). Tool
`fp_approve({lib_id, geometry_hash: ^[0-9a-f]{64}$, user_quote: 8..300})`. `fp_approve` is
exempt from the plan-policy "no active plan step" refusal in `BudgetGate.before_prepare`
(index call 13).

Guards (spec §7.7, all → `approval_not_allowed` with a `details.guard` number):
1. `lib_id` local and `review_state == "needs_user_review"`;
2. `geometry_hash` == current read-back hash;
3. a `footprint_reviews` row for (conversation, lib_id, hash) with both `rendered_at` and
   `checked_at`;
4. + 6. the user message(s) of the current run (`messages.run_id == ctx.run_id`, role
   `user`) include one with `metadata.origin == "user"` created after both timestamps; no
   plan of this conversation is `running`;
5. `user_quote` is a substring of that message after whitespace normalisation
   (`" ".join(s.split())`, case-sensitive), starts and ends on word boundaries, and holds
   ≥ 2 words and ≥ 8 non-space characters — or equals the whole normalised message when
   that is shorter (index call 15);
7. `run_checks` on the current read-back has no error.

Apply (under the adapter's library lock, `FootprintChangeSet` with `library` set):
`cleanup_pending` → `insert_pending(op="approve")` → open the document and recompute
`geometry_hash` **before** `begin()`; ≠ `args.geometry_hash` → `approval_not_allowed`
(guard 2, TOCTOU) → commit + save → read-back hash must equal `args.geometry_hash` →
`finalise(done, post_sha256)`.
Effect: one commit on the footprint document (`needs_user_review = no`, new field
`User review = "approved <YYYY-MM-DD>, conversation <id8>, geometry <hash8>"`), save,
read-back (hash unchanged), journal `approve` row, change-log kind `footprint_approved`
with `user_quote` in `after`, `Logical(1)`, `library_changed={nick}`.

- [ ] **Step 1: Failing unit tests** (`test_approve_guards.py`, DB + fake geometry):
  `test_same_turn_refused` (render, check and approve in one run: the run's message predates
  the render) — guard 4; `test_wrong_hash` — 2; `test_no_render`, `test_no_check` — 3;
  `test_check_errors` — 7; `test_server_message_does_not_count` parametrised over the
  plan-execute message (`plans/service.py`), the close-session prompt (`origin =
  "close-session"`) and a notice → 4; `test_quote_not_in_message` — 5;
  `test_whitespace_normalised_quote_ok`; `test_trivial_quote_refused` (`"e"`, `"ok"`);
  `test_quote_word_boundary` (`"ok go"` inside `"look good"` refused);
  `test_short_whole_message_ok` (message `"ok!"`, quote `"ok!"`);
  `test_approve_with_policy_always` (no active plan step, policy `always` → allowed);
  `test_geometry_changed_between_prepare_and_apply` → guard 2; `test_plan_running` — 6; `test_not_local`,
  `test_already_approved` — 1; `test_happy_path_effect` (fields, journal, change log,
  budget 1). `test_ws.py::test_submit_stamps_origin_user`: a WS send stores
  `metadata.origin == "user"`; the plan-execute submit does not.
- [ ] **Step 2: Failing kicad tests** (`test_fp_approve.py`, `repo_session`): happy path after
  `fp_create` + `fp_render` + `fp_check` + a simulated human message (inserted through
  `engine.submit_ex(..., metadata={"origin": "user"})` and a new run) → `review ==
  "approved"`, `geometry_hash` unchanged across approval (spec §16.1);
  `test_refused_after_replace` (replace changes the hash → guard 2).
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(footprints): fp_approve with server-side guards`.

### Task 2: `fp_delete` and the sibling-project scan

**Interfaces:** `scan_other_projects(repo_root: Path, own_project_dir: Path, lib_id: str)
-> ScanResult(hits: list[str], complete: bool)` in `usage.py` (blocking; run with
`asyncio.to_thread`): walks the repo root, skips hidden dirs, `SKIP_DIRS` and symlinks,
**descends** into dirs with their own `*.kicad_pro`, skips only the own project's document
files (`document_files(own project)`, not its subtree — a project at the repo root would
otherwise hide every nested sibling), streams `*.kicad_sch`/`*.kicad_pcb` in 1 MB chunks with
`len(needle)` overlap and searches `b'"' + lib_id + b'"'`; caps 500 files, 20 MB per file,
200 MB in total and 20,000 directories visited (any cap → `complete=False`). Tool
`fp_delete({lib_id})`: prepare refuses `not_found` (incl. a cleanable pending copy),
`not_local`, `in_use` (refs of the open project in `details.refs`); scan hits → note
`possibly_in_use_elsewhere` (with `complete`); apply = cleanup → journal `delete` →
`DeleteLibraryItem` → read-back gone → finalise `post_sha256 = absent`; `Logical(1)`, not
counted in `MAX_PREEXISTING_SYMBOL_DELETES`; change-log kind `footprint_deleted`.

- [ ] **Step 1: Failing unit tests** (`test_usage_scan.py`, `tmp_path`): a sibling project's
  `*.kicad_sch` containing the lib id is found; the own project is skipped; a symlinked dir
  is not followed; 501 files → `complete=False`; a 21 MB file → skipped, `complete=False`;
  `"X:L1"` does not match `"X:L10"` (quotes in the needle); `test_root_project_finds_nested_sibling`;
  `test_total_bytes_cap` (patch the cap to 1 MB); a match across a chunk boundary is found.
- [ ] **Step 2: Failing kicad tests** (`test_fp_delete.py`): `test_delete_in_use` → `in_use`
  naming `L1`; `test_delete_stock_not_local`; `test_delete_ok` (file gone, `GetLibraryItems`
  without it, journal `done` with `absent`, caches invalidated so `fp_get` → `not_found` in
  the same turn); `test_delete_budget_one_not_preexisting_delete` (unit: `Logical(1)`,
  `deleted_symbols == []`, four `fp_delete` do not hit `MAX_PREEXISTING_SYMBOL_DELETES`).
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(footprints): fp_delete`.

### Task 3: Local footprints in `sch_set_fields` / `sch_place_symbol`, `library_row` step

**Interfaces:** `footprint_notes` (spec §7.9): nickname == local nickname →
tools **off** (`settings.footprint_tools` False, row present or not): only the
`local_names_on_disk()` existence check plus the `footprint_unverified` note — no
activation, no `library_row` stage, no API call; row present (tools on): `activate_footprints()`
and API existence check (`not_found` with `closest` from the local
library), note `footprint_needs_review` unless `review_state == "approved"`; row missing:
`local_names_on_disk()` existence check, `cs.library_row = LibraryRowStage(nick, uri,
create_library=False)`, note `footprint_review_unknown`. Stock nicknames unchanged
(no activation). Adapter (`_two_phase_steps`): after `on_mutation`, when
`cs.library_row` is set, `async with library_lock(dir)`: `run_library_row(ctx, stage, lib,
verify=footprint_name)` (add → read back row → check the footprint exists through the
API → on mismatch delete the row and raise `not_found`), then `spec.apply`; if
`spec.apply` fails, `DeleteLibraryTableEntry` (shielded) and re-raise. Journal `row_add`
like S1. Result gets `side_effects: ["fp_lib_table_row_added"]`; budget 0 for the row.

- [ ] **Step 1: Failing unit tests**: `test_footprint_notes_local.py` (row present → API path +
  `footprint_needs_review`; row missing → disk path + `library_row` + `footprint_review_unknown`;
  tools off with row present and with row missing → disk check + `footprint_unverified`,
  no `library_row`, `activate_footprints` and the API never called; stock footprint on a boardless
  server → `footprint_unverified`, no activation); `test_sdk_tools_library_row.py`
  (`test_row_compensated_when_apply_fails`, `test_row_mismatch_raises_not_found_and_removes_row`,
  `test_row_costs_zero_budget`).
- [ ] **Step 2: Failing kicad tests** (`test_assign_local.py`, `repo_session`):
  - `test_second_project_assigns`: `main` creates L201; switch to `io`; `sch_set_fields` on a
    placed `Device:L` with `<nick>:L_Bourns_SRN3012BTA` → row added (URI
    `${KIPRJMOD}/../../footprints/<nick>.pretty`), `side_effects` names it, note
    `footprint_review_unknown`.
  - `test_row_stage_no_external_change`: next turn start in `io` reports no external
    change and pauses no plan (plan open in the rig).
  - `test_same_turn_create_then_assign`: `fp_create` then `sch_set_fields` with the new lib id
    in one turn → existence check passes (cache invalidated).
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement**; add `fp_approve`, `fp_delete` to `FP_WRITES`
  and assert `FP_WRITES ⊄ board`, `⊄ requirements`.
- [ ] **Step 5: Run** — PASS. **Step 6: Commit** `feat(footprints): assign local footprints`.

---

**Acceptance (F4b done when):** all guards pinned by unit tests; approve/delete/assign
work in `repo_session`; a row added by `sch_set_fields` is undone by History together with
the turn (journal `row_add`); CI green.

**Risks:** message ordering by timestamp (use the DB `created_at` of the same clock;
tests with equal timestamps → refuse); a future server-generated user message (allow-list
by `origin == "user"` keeps it out by construction); scan performance on big repos (caps).

**Do NOT:** let `fp_approve` run in plan execution or without a human message; store
`user_quote` in a KiCad file; parse or write `.kicad_sch` for the scan (byte search only);
set `create_library` in the `library_row` step; act on KiCad GitLab; request Copilot.
