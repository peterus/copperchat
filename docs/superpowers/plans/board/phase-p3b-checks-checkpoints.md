# Phase P3b — `pcb_check`, board checkpoints, `pcb_approve` and its guards

> Index contracts: B-C10 (produces, exactly), B-C14 (`placement_not_approved`, `not_approved`,
> `approval_not_allowed`); consumes B-C2, B-C3, B-C7, B-C8, B-C9. Index calls 7, 14. Spec §7.1
> (`pcb_check`, `pcb_status` checkpoint fields), §7.2 (`pcb_approve`), §7.3, §8.3, §12 (M3
> accepted warnings, M14). Footprints spec §7.7 / F4b Task 1 (guards). **Wave 4: starts when
> P3a and P2 are merged** (index "Depends on"); its kicad tests use P2's tools
> (`pcb_sync_from_schematic`, `pcb_set_outline`, `pcb_set_rules`).

**Goal:** the server knows, per board file, which placement and which board the user approved
on which evidence; `pcb_check` produces that evidence; `pcb_approve` records an approval only
with the user's own words after they saw it; later tools can refuse on a stale checkpoint.

`cc/` = `backend/src/copperchat/`.

**Files:**
- Create: `cc/db/migrations/0013_board.sql`, `cc/db/repos/board.py`, `cc/approvals.py`,
  `cc/kicad_tools/board/{check,approve,checkpoints,specs_check}.py`
- Modify: `cc/kicad_tools/footprints/approve.py` (use `cc/approvals.py`, re-export
  `normalise`, `quote_in_message`), `cc/editing/gate.py` (`PLAN_EXEMPT_TOOLS`),
  `cc/kicad_tools/board/{status,views,specs}.py`, `cc/changes/repo.py` (add the
  in-transaction helper `ChangeRepo.add_many_in_transaction` only; `add_many` reuses it)
- Tests: `backend/tests/unit/db/test_board_repo.py`, `backend/tests/unit/test_approvals.py`,
  `backend/tests/unit/board/{test_check_parse,test_approve_guards,test_checkpoints}.py`,
  `backend/tests/kicad/board/{test_check,test_approve,test_status_checkpoints}.py`,
  `backend/tests/unit/changes/test_change_repo_tx.py`

**Shared kicad setup** (`tests/kicad/board/conftest.py`, append): module-scoped
`synth_setup` = `synth_session` + P1a's `board_ctx` + through the real two-phase adapter:
`pcb_sync_from_schematic`, `pcb_set_outline {shape: "rect", width: 60, height: 45,
corner_radius: 1, holes: {inset: 3}}`, `pcb_set_rules {layers: 4, preset: "jlcpcb-4l"}`;
helper `user_says(rig, run_id, text)` exactly like `tests/kicad/footprints/test_fp_approve.py`
(`MessageRepo.insert(..., metadata={"origin": "user"})` + `on_turn_start`).

---

### Task 1: Migration, repositories, shared approval helpers

**Interfaces:** `0013_board.sql` = the B-C10 SQL verbatim (with a header comment naming
B-C10). `cc/db/repos/board.py`: `@dataclass(frozen=True) Checkpoint(id, project_id,
board_file, stage, hash, conversation_id, run_id, user_quote, approved_at)`,
`Review(id, project_id, board_file, conversation_id, stage, placement_hash, board_hash, run_id,
kind, view, check_level, hard_failures, m1_pass, created_at)`;
`BoardCheckpointRepo(db).approve(project_id, board_file, stage, hash, conversation_id,
run_id, user_quote) -> int`, `.latest(project_id, board_file, stage) -> Checkpoint | None`
(highest `id`); `BoardReviewRepo(db).record_render(project_id, board_file, conversation_id,
run_id, *, view, placement_hash, board_hash) -> int`, `.record_check(..., *, level,
hard_failures, m1_pass: bool, placement_hash, board_hash, stage) -> int`,
`.evidence(project_id, board_file, conversation_id, *, placement_hash=None,
board_hash=None) -> list[Review]` (ordered by `id`), `.last_full_check(project_id,
board_file, board_hash) -> Review | None`; timestamps `utcnow_iso()` (`cc/db/__init__.py`).
`cc/approvals.py` (moved from `footprints/approve.py`, behaviour unchanged):
`MIN_QUOTE_WORDS = 2`, `MIN_QUOTE_CHARS = 8`, `normalise(text)`, `quote_in_message(quote,
message) -> bool`, `async human_messages_after(ctx, since_iso: str | None) ->
list[MessageRow]` (the current run's user messages with `metadata.origin == USER_ORIGIN`,
`created_at > since_iso` when given — the old `_human_messages` is this with `None`),
`async plan_running(ctx) -> bool` (`PlanRepo.running_in_conversation`; `False` without
conversation). `board_file(project) -> str` in `board.py`: `pcb_file` relative to
`project.dir`, POSIX.

- [ ] **Step 1: Failing unit tests** (`test_board_repo.py`, temp DB through the normal
  migration runner): `test_migration_0013_applies` (fresh DB and a DB migrated to 0012);
  `test_stage_check_constraint` (`stage='x'` → `IntegrityError`); `test_kind_and_level_checks`;
  `test_latest_is_highest_id`; `test_evidence_filters_conversation_and_hash`;
  `test_last_full_check_newest`. `test_approvals.py`: `test_quote_rules` (whole short message,
  2 words/8 chars, word boundaries — the F4b cases), `test_human_messages_after_filters_origin`
  (`plan`, `close-session` origins and older messages excluded), `test_plan_running`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/db tests/unit/test_approvals.py -q` — FAIL.
  **Step 3: Implement**; `footprints/approve.py` imports the helpers and keeps the names
  `normalise`, `quote_in_message` importable (existing tests import them). **Step 4: Run**
  — PASS, and `uv run pytest tests/unit/footprints -q` passes **unchanged** (no test file
  edited; `git diff --stat tests/unit/footprints tests/kicad/footprints` empty).
- [ ] **Step 5: Commit** `feat(board): 0013_board migration, repos, shared approval helpers`.

### Task 2: `pcb_check` and render evidence

**Interfaces:** `check.py`: `async pcb_check(ctx, args) -> ToolResult`;
`parse_cli_drc(report: dict) -> tuple[tuple[DrcMarker, ...], tuple[DrcMarker, ...], int]`
(violations, parity, unconnected) for the `kicad-cli pcb drc --format json` document (keys
`violations[].{type, severity, description, items[].uuid, items[].pos}`,
`unconnected_items`, `schematic_parity`); `verdict(report: MetricsReport) -> Literal["pass",
"review","fail"]`. Schema `_obj({"level": {"enum": ["quick","full"]}}, ["level"])`, read tool
(no edit lock, cost 0). **quick:** `board_model` → `run_drc_task` (`session.call(..., timeout=drc_call_timeout(60))`, B-C4) → `read_board_doc` →
`run_cpu(metrics_job, model, doc, drc, None, None, parity=None, timeout_s=30)` (P3a's job:
builds the classification and calls `compute_metrics` in the worker; preset `None` → M14
`n/a`). **full:**
additionally a copy dir `agent_dir(project)/"board"/"check"/<uuid>/` with
`session.save_copy("sch", dir/<stem>.kicad_sch)` (copies sub-sheets),
`session.save_copy("pcb", dir/<stem>.kicad_pcb)` (copies `.kicad_dru`) and byte copies of
`<stem>.kicad_pro`, `fp-lib-table`, `sym-lib-table` when present (read-only inputs; nothing
in the project is written); `async with BOARD_CLI_LOCK:` (P1c, process-wide, shared with
`pcb_render`) `run_kicad_cli(settings, ["pcb", "drc", "--format", "json",
"--severity-all", "--schematic-parity", "-o", <dir>/drc.json, <copy>],
timeout=BOARD_PARITY_TIMEOUT_S)` (120 s); `TimeoutError` or a non-zero exit with no JSON →
`StructuredToolError(code="drc_rejected")` (timeout named in the message); the copy dir is
removed in `finally`. In full mode the metrics come from `run_cpu(metrics_job, model, doc,
drc, preset_for(model, doc), None, parity=<CLI parity>, timeout_s=30)` — never a direct
`compute_metrics` in `run_cpu`. **One source per M3 term:** errors and warnings come only
from the API DRC task result (`DrcResult`, also M2); parity markers come only from the CLI
JSON's `schematic_parity`; the CLI's non-parity `violations` are **not** counted (no
double count) — they are only cross-checked against the API markers by `(type, KIID set)`
and the mismatch count is logged and reported as `cli_crosscheck {only_cli, only_api}`
(informational, never in a metric). M14 uses the preset (P3a). Tool-placed holes carry
`not_in_schematic` (P2), so they raise no parity marker. Hashes are read before the DRC and again after; when they differ
the result says `"stale": true` and **no row is recorded**. Result: `level`, `errors` (≤ 50
+ omitted), `warnings` (each with `accepted: bool`, ≤ 50 + omitted), `parity` (≤ 50),
`metrics`, `hard_failures`, `verdict`, `placement_hash`, `board_hash`, `seconds`. Recording
(index call 14, only with `ctx.conversation_id` and `ctx.editing`): `record_check(level,
hard_failures, m1_pass = (M1 verdict == "pass"), stage = "placement" if level == "quick"
else "board", both hashes)`. `views.py` (P1c's `pcb_render`): after a successful render
`record_render(view, placement_hash, board_hash)` from the `BoardRender`, same condition.

- [ ] **Step 1: Failing unit tests** (`test_check_parse.py`): `test_parse_cli_report` (an
  inline JSON string shaped like kicad-cli's output: 2 violations, 1 parity, 3 unconnected →
  markers with KIIDs, `unconnected == 3`); `test_verdict` (any fail → `fail`, else any review
  → `review`, else `pass`); `test_schema` (`level` required, `"medium"` rejected).
- [ ] **Step 2: Failing kicad tests** (`test_check.py`, `synth_setup`):
  `test_quick_records_row` (one `check` row, level `quick`, `m1_pass == 0` — parts are still in
  staging —, hashes equal `pcb_status`'s); `test_full_runs_parity_and_m14` (`parity` present,
  M14 not `n/a`, `seconds` recorded with `record_property`); `test_accepted_warning_marked`
  (write a `board.md` Checks row for a warning type present on the board through
  `ctx.docs.patch_section` → that warning `accepted: true`, M3 no longer counts it);
  `test_full_hard_failures_recorded` (`hard_failures` equals the report's count);
  `test_full_check_m3_sources` (monkeypatch `run_kicad_cli` to write a JSON with 2
  non-parity violations and 1 parity marker, the API `DrcResult` stubbed with 1 error and 0
  warnings → M3 value == 2 (1 API error + 1 parity), the CLI violations appear only in
  `cli_crosscheck`, not in `errors`);
  `test_render_records_evidence` (`pcb_render view=placement` → one `render` row with the
  view and both hashes); `test_no_conversation_no_rows` (`conversation_id=None` → table
  empty); `test_copy_dir_removed`; `test_full_parity_uses_board_cli_lock` (spy: the parity
  DRC runs inside `BOARD_CLI_LOCK` with `timeout=120`; a patched `TimeoutError` →
  `drc_rejected`, copy dir removed); `test_check_boardless` (`no_board`).
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement**; register `pcb_check` in
  `specs_check.py` → `board/specs.py`. **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(board): pcb_check quick/full with recorded evidence`.

### Task 3: `pcb_approve`, guards, checkpoint enforcement

**Interfaces:** `checkpoints.py`: `CheckpointState = TypedDict("CheckpointState", {"approved":
bool, "stale": bool, "hash": str | None, "approved_at": str | None, "conversation_id": str |
None})`; B-C10 `checkpoint_state(db, project, model) -> dict[str, CheckpointState]`
(`approved` = a latest row exists, `stale` = it exists and its hash ≠ the current hash of that
stage); `require_placement_approved(ctx, model)` → `StructuredToolError(code=
"placement_not_approved", details={"stale": bool, "placement_hash": current})` unless the
latest `placement` row's hash == `placement_hash(model)`; `require_board_approved(ctx,
model)` → `not_approved` unless the latest `board` row's hash == `board_hash(model)` **and**
`last_full_check(board_hash)` exists with `hard_failures == 0` (`details.reason` ∈
`missing|stale|check_failed`); both fail closed without `ctx.editing` (no DB).
`approve.py`: `prepare_approve(ctx, args) -> BoardChangeSet`, `apply_approve(ctx, cs) ->
ToolResult`, registered with `edit_spec("pcb_approve", …, prepare, apply)`; schema
`{stage: enum placement|board, hash: ^[0-9a-f]{64}$, user_quote: 1..300 chars}` (all
required). `cc/editing/gate.py`: `PLAN_EXEMPT_TOOLS = frozenset({"fp_approve",
"pcb_approve"})` (spec §7.3). Change set: `BoardChangeSet(tool="pcb_approve",
kind="board_approve", board_stages=[], logical=Logical(1))`; apply does **not** call
`apply_board_change_set` (index call 7): it reloads the model, then inside **one**
`async with db.transaction():` (`cc/db/__init__.py`, `BEGIN IMMEDIATE`; repo `execute` calls
in the owning task join it) re-checks guard 1 (TOCTOU) and guard 4 (a write may have landed
between prepare and apply) **before** any insert, then inserts the checkpoint row and the one
change entry (`kind="board_approve"`, `op="add"`, `uuid=f"board-checkpoint-{id}"`,
`sheet_path=""`, `after={stage, hash, user_quote}`) through
`ctx.editing.changes.add_many_in_transaction` — not `add_many`, which opens its own
`db.transaction()` (`cc/changes/repo.py`) and would raise `RuntimeError("nested transactions
are not supported")` (`cc/db/__init__.py` `_transaction`), and not `record_applied`, which
swallows errors and would let the checkpoint row survive a failed change entry. A refused
guard or any exception rolls both back.
`cc/changes/repo.py` (this phase adds only this helper): `async def
add_many_in_transaction(self, entries) -> int` holds the existing insert loop (same SQL, same
`created_at or now` default) and raises `RuntimeError` unless the calling task owns the open
transaction (`self._db._in_own_transaction()`); `add_many` becomes `async with
self._db.transaction(): return await self.add_many_in_transaction(items)` (empty input still
returns 0 without opening a transaction) — no behaviour change for existing callers. After the commit
the `on_changes_recorded` listeners are notified (as `EditingService.record_changes` does,
failures logged), and apply returns `ToolResult(data={stage, hash, approved_at, next},
changed_docs=set())`.

Guards (all → `approval_not_allowed` with `details.guard`, evaluated in this order):
1. `hash` equals the current hash of `stage` (`placement_hash` / `board_hash`); a
   placement approval carrying the current `board_hash` (or vice versa) is refused with
   `details.other_stage = true`;
2. the stage's evidence exists **in this conversation for this hash**: placement = a
   `render` row with view `placement` and a `check` row (quick or full) for the
   `placement_hash`; board = a `full` check row plus `render` rows with views `top` **and**
   `bottom` for the `board_hash`;
3. the evidence passes: placement — the newest check row for the hash has `m1_pass = 1`;
   board — the newest full check for the hash has `hard_failures = 0`;
4. **no write after evidence** (ABA guard, mirrors `fp_approve`'s `last_geometry_write_at`
   check, `cc/kicad_tools/footprints/approve.py` guard 4, which refuses when
   `min(rendered_at, checked_at) <= written_at`): `ChangeRepo.since(project.id, oldest
   evidence created_at)` — the **oldest** of the evidence rows used by guards 2–3 (per required
   kind the newest row for the hash; then the minimum over them: placement = min(placement
   render, check); board = min(full check, `top` render, `bottom` render)) — holds no entry whose `kind ∈ BOARD_KINDS − {"board_approve"}` (one board per project; board
   entries carry `sheet_path == ""`) — a move away and back to the same hash, in the approving
   turn or after the evidence in an earlier turn, means the user never saw the board as it is
   now; suggestion: render + check again, show them, end the turn;
5. `human_messages_after(ctx, newest evidence created_at)` is non-empty (a message the user
   typed, processed by this run, later than the render and the check — so same-turn
   approvals and server-generated messages never count);
6. `quote_in_message(user_quote, m.content)` for one of those messages;
7. `not plan_running(ctx)`.

- [ ] **Step 1: Failing unit tests** (`test_approve_guards.py`, temp DB, a fake model
  provider returning fixed hashes, rows inserted through the repos):
  `test_same_turn_approval_refused` (render + check rows after the run's user message → 5);
  `test_server_generated_message_does_not_count` (parametrised origins `plan`,
  `close-session`, none → 5); `test_quote_not_substring` (6); `test_trivial_quote` (`"ok"`
  inside a longer message → 6); `test_placement_with_board_hash_refused` (1,
  `other_stage`); `test_wrong_hash` (1); `test_plan_running_refused` (7);
  `test_aba_in_approving_turn_refused` (evidence rows for hash A; user message; then two
  `board_move` change entries newer than the evidence — away and back, current hash A again
  → 4); `test_write_before_evidence_ok` (a `board_move` entry older than the evidence → no
  guard 4); `test_write_between_render_and_check_refused` (placement render row for hash
  A, then a `board_move` entry, then the check row for hash A (moved back) → the write is
  newer than the render, older than the check → 4; same for the board stage with the write
  between the `top` render and the full check); `test_approve_entry_does_not_count` (an older approval's `board_approve` entry
  newer than the evidence → not 4);
  `test_no_placement_render` (2); `test_evidence_other_conversation` (2);
  `test_m1_failing` (3); `test_full_check_with_hard_failures` (3);
  `test_missing_bottom_render` (2); `test_board_needs_full_not_quick` (2);
  `test_happy_placement`, `test_happy_board` (row written, change entry kind
  `board_approve`, `Logical(1)`, `changed_docs == set()`);
  `test_hash_changed_between_prepare_and_apply` (1); `test_write_between_prepare_and_apply`
  (a `board_move` entry inserted after prepare → apply refuses with 4, no row written);
  `test_apply_guard_refusal_leaves_no_rows` (guard 1 and, separately, guard 4 failing at
  apply → `board_checkpoints` and `changes` row counts unchanged);
  `test_apply_real_path_commits_both` (no monkeypatch of the change repo: a real temp DB,
  apply runs through `add_many_in_transaction` inside its one `db.transaction()` → no
  `RuntimeError`, one checkpoint row and one `board_approve` change row committed);
  `test_apply_change_entry_failure_rolls_back` (failure injected **after** the change
  insert: wrap the real `add_many_in_transaction` so it inserts, then raises → the error
  propagates, neither the checkpoint row nor the change row stays);
  `test_add_many_in_transaction_requires_open_tx` (`tests/unit/changes/test_change_repo_tx.py`: outside a
  transaction → `RuntimeError`; `add_many` still writes in its own transaction);
  `test_plan_policy_always_allowed`
  (`PlanGate`/`BudgetGate.before_prepare` pass `pcb_approve` without an active step).
  `test_checkpoints.py`: `test_state_missing`, `test_state_stale`, `test_only_latest_counts`
  (rows A then B; current = A → stale), `test_require_board_check_failed`,
  `test_require_fails_closed_without_db`.
- [ ] **Step 2: Failing kicad tests** (`test_approve.py`, `synth_setup`; turn 1 = tool calls,
  turn 2 starts with `user_says`): `test_placement_approval_flow` (the test moves every part
  except the locked mounting holes inside the outline through one board commit — a column
  grid of courtyard boxes with 1 mm gaps inside the outline **inset 6 mm on every side**, so
  it stays clear of the four corner hole courtyards (hole inset 3 mm, courtyard r ≈ 2.45 mm);
  the test first asserts `pcb_metrics {only: ["M1"]}` → M1 verdict `pass` (value 0); then
  render placement + quick check (its `m1_pass == 1`) → user says "looks good, approve
  placement" → `pcb_approve` → `require_placement_approved` passes);
  `test_aba_kicad_refused` (after the evidence, in turn 2, move one part 1 mm and back
  through two board change sets → `pcb_approve` with the unchanged hash →
  `approval_not_allowed`, `details.guard == 4`); `test_older_approval_not_revived` (approve at hash A;
  move one part 1 mm through a board commit → hash B; render + check + approve B; move the
  part back → hash A again → `require_placement_approved` raises `placement_not_approved`
  with `details.stale`, `checkpoint_state["placement"]["stale"] is True`);
  `test_dru_change_invalidates_board` (board evidence: `pcb_check full` without hard failures
  is not reachable on an unrouted board, so the rows of the current `board_hash` are recorded
  through the repo; approve board; then `pcb_set_rules {custom_rules: [one rule with a
  reason]}` → `board_hash` differs, `require_board_approved` → `not_approved` with
  `reason == "stale"`); `test_placement_move_invalidates` (approved, then a 0.01 mm move →
  refused); `test_approval_survives_new_session` (reopen the session → still valid);
  `test_approve_budget_cost_1`.
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(board): pcb_approve with server-side guards and checkpoints`.

### Task 4: `pcb_status` checkpoint fields

**Interfaces:** `status.py` (P1c) adds `checkpoints` = `checkpoint_state(...)` (both stages,
with `stale`) and `last_check` = `{level, hard_failures, m1_pass, created_at, for_current:
bool}` of the newest `check` row for this board file in this conversation (or `null`);
without a DB both fields are `null`. Still a read tool.

- [ ] **Step 1: Failing kicad tests** (`test_status_checkpoints.py`): `test_status_no_approvals`
  (both `approved: false`); `test_status_stale_after_move`; `test_status_last_check`
  (`for_current` true right after `pcb_check quick`, false after a move).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): pcb_status checkpoint and last-check fields`.

### Task 5: Docs + index as built, checks, PR

- [ ] Index "As built (P3b)": guard numbering (1–7 above, 4 = no write after evidence),
  `checkpoint_state` field names, `human_messages_after` signature, `pcb_check full` copy-dir
  approach and measured times (quick, full) on synth, `PLAN_EXEMPT_TOOLS` change.
  CHANGELOG `[Unreleased]`: "Board checks and approvals (`pcb_check`, `pcb_approve`) — behind
  COPPERCHAT_BOARD_TOOLS."
- [ ] Run `uv run pytest -m "not kicad"`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy src`, `uv run pytest -m kicad tests/kicad/board tests/kicad/footprints` in the
  kicad-base image (with `timeout`).
- [ ] PR `board phase P3b: checks, checkpoints, approvals`; review-loop per the index's Phase
  Conventions (dimension `security` covers the guards: Review Focus 5).

## Acceptance (P3b)

- [ ] CI `backend` and all 8 `kicad (…, i/8)` shards green; footprint approval tests unchanged
  and green.
- [ ] `test_older_approval_not_revived`, `test_dru_change_invalidates_board`,
  `test_same_turn_approval_refused`, `test_server_generated_message_does_not_count`,
  `test_quote_not_substring`, `test_trivial_quote`, `test_placement_with_board_hash_refused`,
  `test_plan_running_refused`, `test_aba_in_approving_turn_refused` and the four
  evidence-missing tests pass.
- [ ] `pcb_check full` on synth runs parity and M14; quick/full times recorded.
- [ ] `pcb_status` shows both checkpoints with stale flags and the last check.
