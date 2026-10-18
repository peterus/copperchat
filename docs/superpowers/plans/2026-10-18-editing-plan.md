# CopperChat Schematic Editing Implementation Plan (index)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> This file is the **index**. Each phase lives in its own file under
> `docs/superpowers/plans/editing/` and is executed by one child session as
> **exactly one PR**. A phase executor reads, in this order: this index, the
> spec, the spike docs its phase names, then its own phase file. The foundation
> index (`2026-10-15-foundation-plan.md`, C1–C8, C7a, X1–X20) stays binding for
> everything this plan does not change.

**Goal:** Build sub-project 2 "Schematic editing": the agent places parts from
the libraries, connects them, labels nets, edits fields, moves/deletes/duplicates,
annotates and works with hierarchical sheets — only through KiCad's IPC API,
verified by read-back, planned before larger changes, visible to the user and
undoable.

**Architecture:** Mutating agent tools become two-phase (`prepare` reads and
computes a `ChangeSet`, `apply` writes it in KiCad commits and verifies by
read-back against the live `GetSchematicNetlist`, compensating on mismatch).
A per-turn editing state carries the logical change budget; a plan service
gates edits while a plan is open and checkpoints every plan step. A change log
recorded by the tools, a `kicad-cli sch diff` service and a patched KiCanvas
show the user what changed.

**Tech Stack:** unchanged from the foundation (Python 3.13, FastAPI, kipy at
`87b945a0`, KiCad `0616634034`, `claude-agent-sdk==0.2.168`, React 19 / zustand /
zod, KiCanvas fork `peterus/kicanvas` branch `copperchat`).

**Spec:** `docs/superpowers/specs/2026-10-17-editing-design.md` (amended after
the spikes, 2026-10-18). Spikes: `docs/spikes/2a-sch-write.md`,
`2b-libraries.md`, `2c-diff-highlight.md`, `2d-agent-eval.md`. Their harness code
under `spikes/2a-sch-write/`, `spikes/2b-libraries/`, `spikes/2d-agent-eval/` is
the **reference implementation** for many tasks (named per task) — port it, do
not import it.

---

## Phase overview

| # | Phase file | Goal (one line) | Depends on |
|---|---|---|---|
| E1 | `editing/phase-e1-session-core.md` | `KicadSession.commit()`, save listeners + fresh export jobs, `save_copy` saves first, symbol libraries eager, live netlist, `blank` fixture, migration `0006`, editing settings | — |
| E2 | `editing/phase-e2-edit-framework.md` | geometry + scene, `ChangeSet`/two-phase adapter (read-only refusal, gate, budget hook), apply/verify/compensate engine, change recording, `DocumentInconsistent`, annotation allocator, `sch_get_pin`, `sch_add_wire` | E1 |
| E3 | `editing/phase-e3-library.md` | library index (symbols eager, footprints lazy per library, derived-symbol pin probe), `lib_*` tools, part preferences, `parts_get_notes` | E1 (independent of E2) |
| E4 | `editing/phase-e4-place-fields.md` | `sch_place_symbol`, `sch_set_fields`, `sch_find_space`, `sch_annotate` | E2, E3 |
| E5 | `editing/phase-e5-connect.md` | router (T-joins), `sch_connect`, `sch_add_label`, `sch_add_power`, `sch_no_connect`, `sch_rename_net`, `sch_reroute` | E2 (test parts placed with E2's `place_raw` helper, not E4) |
| E6 | `editing/phase-e6-move-delete-duplicate.md` | `sch_move` (drag), `sch_delete` (attached items, sheets), `sch_duplicate` | E4, E5 |
| E7 | `editing/phase-e7-sheets.md` | `sch_add_sheet`, `sch_add_sheet_pin` | E2 |
| E8a | `editing/phase-e8a-checks-checkpoints.md` | step checkpoints (`kind=step`), ERC baseline/delta, turn-end ERC, change REST/WS, `sch_changes`, diff service | E2 |
| E8b | `editing/phase-e8b-plans.md` | plans: model, state machine, plan gate + budget, plan tools, REST, `plan-updated` | E8a (E4, E5 for its end-to-end test) |
| E9a | `editing/phase-e9a-viewer-changes.md` | KiCanvas fork highlight overlay, Changes panel, diff layer, before/after | E8a |
| E9b | `editing/phase-e9b-plan-ui.md` | plan card, history nesting, Settings › Editing | E8b, E9a |
| E10 | `editing/phase-e10-skills-scenarios.md` | `kicad-schematic-editing` skill, style recipes, golden scenario tests, undo/round-trip tests, live eval, docs | E6, E7, E8b |

Execution waves (a phase starts once every phase it depends on is merged):

| Wave | Phases | Note |
|---|---|---|
| 1 | E1 | |
| 2 | E2, E3 | |
| 3 | E4, E5, E7, E8a | E4 needs E3 too |
| 4 | E6, E8b, E9a | |
| 5 | E9b, E10 | |

Parallel phases touch the same few registration points (registry
`_PROVIDERS`, `web/app.py` routers, `protocol-fixtures/`, `frames.ts`,
`panels/index.ts`); each adds lines only. Rebase on `main` before merge and keep
both sides of such one-line conflicts.

**No go/no-go gates remain**: all four spikes passed (2A partial = go with
workarounds, 2B partial, 2C go, 2D "tool shape OK, needs changes"). Their
workarounds are part of the phase files.

---

## Global Constraints

Every task implicitly includes these (plus the foundation's Global Constraints).

- **Write path = KiCad IPC API only.** No file text edits, no S-expression
  generation, no `ParseAndCreateItemsFromString`, no selection/clipboard
  commands. `kicad-cli` only for ERC, netlist/diff checks and exports.
- **Fixtures are KiCad-generated**: copied verbatim from KiCad (demos,
  templates), upgraded with `kicad-cli … upgrade`, or created/edited through the
  API (`create_document`, tools). Never hand-edited — also not in tests.
- Pinned KiCad `0616634034fa1a8c92c141c293342cbef2f68dff`, kipy `87b945a0`.
- Coordinates in **mm**, snapped to the **1.27 mm** grid; full UUIDs; every
  mutating tool states exactly what it changed (created / modified / deleted,
  incl. KiCad cleanup side effects) and reports success **only after read-back
  verification**.
- **Change budget (owner, 2026-10-18):** counted in **logical changes**; limit
  **20 per turn** outside a plan; **≤ 3 deletions of symbols that existed before
  the turn**; a part the agent placed and deletes again in the same turn counts
  for neither.
- **Libraries (owner, 2026-10-18):** all **symbol** libraries loaded eagerly
  (~1.5 GB RSS); **footprint** libraries lazily, one library per request.
- A plan open (`proposed`/`approved`/`paused`) → every mutating call refused
  (`plan_open`); plan `running` → mutating calls only inside an active step.
- Read-only session → every mutating call refused (`read_only`) **before** any
  snapshot.
- The main agent keeps `tools=["Skill"]` + CopperChat MCP tools only.
- Spike workarounds are mandatory where a phase names them: definition-less
  symbol updates; canonical orientation/mirror forms; `for_sheet(instance)` for
  every sub-sheet read/write; read-back of the neighbourhood after each push;
  export jobs only after save + `close_document`/`open_document`.

## Review Focus

Failure modes most likely to bite a user although no single happy-path test
hits them. Each names the owning task.

1. **A connection that looks made but is not, or a silent short** (wire end
   0.01 mm off a pin, wire interior over a foreign pin end, label beside a
   wire) — the tool fails, the document's UUID set and netlist are unchanged.
   Tests: E2 Task 4 (`sch_add_wire`), E5 Tasks 1, 2.
2. **A write lands in a stale commit** after Stop / cancellation / a KiCad
   hang between `begin_commit` and push — the next write must apply normally and
   never be staged into the dropped commit. Tests: E1 Task 1.
3. **Edits invisible to later steps**: sub-sheet edits through the unbound
   schematic (silently lost), export jobs (agent view, KiCanvas, ERC) showing the
   pre-edit state, a new sheet missing from `save_copy`. Tests: E1 Tasks 2, 3;
   E2 Task 3 (sub-sheet), E7 Task 1.
4. **Refusal paths that still take a snapshot or half-apply** (read-only,
   budget, open plan, multi-pin call failing on its 3rd pin) — nothing changed,
   no snapshot, structured error. Tests: E2 Task 2, E5 Task 3, E8b Task 2.
5. **Plan and design drift apart** (server restart, KiCad crash, external
   change, rollback while a run is active) — plan `paused` with a reason,
   rollback refused `agent_busy`, recovery offers the right checkpoint. Tests:
   E8b Tasks 3, 4; E8a Task 1.

---

## Repository layout (additions)

```
backend/src/copperchat/
  db/migrations/0006_editing.sql        # E1 (all SP2 tables, one migration)
  db/repos/editing_settings.py          # E1
  kicad_session/session.py              # E1: commit(), listeners, refresh_jobs, libraries
  kicad_tools/access.py                 # E1: live_netlist()
  kicad_tools/edit/                     # E2 core, E4–E7 tools
    geometry.py scene.py changeset.py engine.py verify.py annotate.py
    specs_core.py (E2) place.py fields.py space.py specs_place.py (E4)
    router.py connect.py labels.py specs_connect.py (E5)
    transform.py delete.py duplicate.py specs_transform.py (E6)
    sheets.py specs_sheets.py (E7)
  library/ index.py service.py packages.py prefs.py specs.py   # E3
  editing/ state.py gate.py service.py hooks.py               # E2 (E8a/E8b extend hooks)
  changes/ repo.py erc.py diff.py specs.py                    # E1 repo, E8a rest
  plans/ model.py machine.py repo.py service.py gate.py specs.py   # E8b
  web/routes_editing.py (E1 settings, E8a changes) routes_plans.py (E8b)
  skills/builtin/kicad-schematic-editing/ (E10) part-preferences/ (E3)
backend/tests/fixtures/projects/blank/   # E1, KiCad-generated
backend/tests/kicad/edit/               # real-KiCad edit tests (E2–E10)
frontend/src/
  stores/changes.ts stores/plans.ts (E9a/E9b)  panels/ChangesPanel.tsx (E9a)
  components/plan/PlanCard.tsx (E9b)  pages/settings/EditingSection.tsx (E9b)
scripts/make-blank-fixture.py           # E1 (runs in the kicad-base image)
```

`library/` replaces the spec's `kicad_tools/edit/library.py` (it is a service
with its own index and warm-up, not a tool helper).

---

## Interface Contracts (SP2)

Binding across phases; a phase may add fields/functions but may not rename or
change these without updating this index in the same PR.

### E-C1. KiCad session additions — `copperchat.kicad_session` (E1)

```python
class KicadSession:
    def commit(self, label: str, *, dry_run: bool = False) -> AsyncContextManager[CommitScope]
        # begin_commit on h.schematic(); body runs session.call()s (incl. for_sheet writes);
        # normal exit -> push_commit(label) (dry_run: drop_commit); any exception incl.
        # CancelledError -> drop_commit under asyncio.shield; drop failing -> restart()
        # + KicadUnavailable. Holds self._commit_lock: save/save_copy/refresh_jobs wait.
        # A pushed commit sets jobs_stale = True and modified = True.
    jobs_stale: bool
    last_saved_at: str | None            # ISO time of the last CopperChat save()
    async def save(self, doc) -> None    # unchanged signature; afterwards awaits every
                                         # manager save listener (hash recording)
    async def refresh_jobs(self) -> None # if jobs_stale: save("sch") + close_document +
                                         # open_document (sch) -> export jobs see edits
    async def save_copy(self, doc, dest) # sch: save() first when is_document_modified()
    async def ensure_symbol_libraries(self, timeout: float = 120.0) -> None
        # idempotent per server start; load_all_libraries(SYMBOL)+wait_for_libraries;
        # kicked off in the background after open_document

@dataclass
class CommitScope:
    label: str
    commit: Any            # kipy Commit (kept: its id is needed to drop it)

class KicadSessionManager:
    def add_save_listener(self, fn: Callable[[Project, DocKind], Awaitable[None]]) -> None
```

`HistoryService` registers its hash recording as a save listener (E1), so
**every** CopperChat save records hashes right after it.

- **E1 (as built):** `CommitScope` also carries `generation` (the server start it
  belongs to): a restart since `begin_commit` discarded the staged items, so the drop
  is skipped. `commit()` refuses a read-only session (`ReadOnlyProject`) and also drops
  when `push_commit` itself fails; the drop runs to completion even when the caller is
  cancelled again meanwhile (the commit lock is released only afterwards); a failing
  drop of a *cancelled* scope restarts and stays a cancellation. Any `session.call`
  of the scope's task after a server restart inside the scope raises
  `KicadUnavailable` (it would write outside the commit). `save`, `save_copy`,
  `refresh_jobs` and a nested `commit` raise `RuntimeError` when called from inside
  the caller's own open commit or from a save listener (instead of deadlocking).
  `SaveListener = Callable[[Project, DocKind], Awaitable[None]]` (the saved doc: the
  history listener re-records only that document's files + the `.kicad_pro`, so an
  external change to the other document is still reported); listener errors are
  logged. `modified` is the
  session's own flag (pushed commit → True, `save("sch")` / restart → False);
  `save_copy("sch")` and `refresh_jobs` ask KiCad (`is_document_modified()`):
  `refresh_jobs` saves only a modified schematic (never overwrites an unmodified one,
  e.g. stale jobs after the turn-end autosave), closes every open schematic document
  (`get_open_documents`) and reopens the root; a failing reopen restarts the server and
  raises `KicadUnavailable`. A hanging `load_all_libraries` kick-off fails the start.
  `ensure_symbol_libraries` polls `get_library_statuses(LT_SYMBOL)` in short
  `session.call`s (no `wait_for_libraries` holding the session lock for up to 120 s)
  and raises `copperchat.kicad_session.LibrariesLoading` (`code="libraries_loading"`,
  HTTP 503) on timeout; a refused kick-off only logs at start, `ensure_symbol_libraries`
  repeats the request and raises `LibrariesLoading` if KiCad refuses again.
  `begin_commit` / `push_commit` run to completion even when the caller is cancelled
  meanwhile (an opened commit is then dropped, a pushed one kept), and a cancellation
  is never swallowed by a failing drop. `HistoryHooks.on_turn_end` no
  longer records hashes itself (the listener does, after each successful save).

### E-C2. Live netlist — `copperchat.kicad_tools.access` (E1)

```python
PinKey = tuple[str, str, str]      # (sheet_path, symbol_uuid, pin_number)
@dataclass(frozen=True)
class Netlist:
    nets: dict[str, frozenset[PinKey]]
    def net_of(self, key: PinKey) -> str | None
    def partition(self) -> frozenset[frozenset[PinKey]]
def live_netlist(h: KicadHandle) -> Netlist     # sch.get_netlist(); pin KIIDs resolved via
                                                 # placed symbols' definition.pins[].id per sheet path
```

`sheet_path` = `/`-joined KIID path of the sheet instance (root = `/<root uuid>`),
the same string `kicad-cli sch diff` and KiCanvas use (2C). Read tools
(`sch_get_net`, `sch_get_symbol`) switch to it in E1 (output unchanged).

- **E1 (as built):** `live_netlist(h, symbols=None)` optionally reuses the caller's
  `get_symbols()` result. `access.sheet_path_key(path) -> str` builds that string from a
  kipy `SheetPath` (or its proto). `live_netlist` includes power-symbol pins
  (`#PWR`/`#FLG`); `sch_get_net` leaves them out, as kicad-cli's netlist does.
  `sch_get_net`'s `function` follows kicad-cli at the pinned commit (`NAME_number`,
  `""` for unnamed pins) and `type` uses KiCad's canonical pin type names
  (`power_in`, `power_out`, `tri_state` — `access.pin_type_name`, so `sch_get_symbol`
  now says `power_in` where it said `power_input`) without kicad-cli's `+no_connect`
  suffix (no-connect markers are E5's `sch_no_connect` business, read from the scene). Verified against kicad-cli on the
  hierarchy fixture for every net (`tests/kicad/edit/test_live_netlist.py`). The
  export-job netlist (`netlist_nodes`, `parse_netlist_xml`) is gone; tests read
  kicad-cli netlists with `tests.support.kicad_cli`.

### E-C3. Database — `0006_editing.sql` (E1, used by all)

| Table / change | Columns |
|---|---|
| `history_snapshots` + | `kind TEXT NOT NULL DEFAULT 'turn'` (`turn`\|`step`), `plan_step_id TEXT NULL` |
| `editing_settings` | `project_id TEXT PK, plan_policy TEXT DEFAULT 'large'` (`always`\|`large`\|`never`), `change_budget INTEGER DEFAULT 20, annotation_block INTEGER DEFAULT 100` (100\|1000), `show_agent_view INTEGER DEFAULT 1` |
| `changes` | `id INTEGER PK, project_id, run_id, plan_step_id NULL, call_seq INTEGER, tool TEXT, op TEXT` (`add`\|`modify`\|`delete`), `kind TEXT, uuid TEXT, sheet_path TEXT, ref NULL, net NULL, before TEXT NULL, after TEXT NULL` (JSON), `side_effect INTEGER DEFAULT 0, created_at` |
| `erc_results` | `id TEXT PK, project_id, run_id, plan_step_id NULL, baseline_snapshot_id TEXT, new_errors TEXT, new_warnings TEXT, resolved TEXT, justifications TEXT` (JSON), `created_at` |
| `plans` | `id TEXT PK, project_id, title, summary, status, revision INTEGER, event_seq INTEGER DEFAULT 0, pause_reason NULL, base_snapshot_id NULL, created_run_id, created_at, updated_at`; `UNIQUE INDEX ux_plans_open ON plans(project_id) WHERE status IN ('proposed','approved','running','paused')` |
| `plan_steps` | `id TEXT PK, plan_id, idx INTEGER, title, intent, expected_changes INTEGER, touches TEXT (JSON {refs,nets}), verification TEXT, status` (`pending`\|`running`\|`done`\|`failed`\|`interrupted`), `run_id NULL, checkpoint_id NULL, change_count INTEGER DEFAULT 0, summary NULL, failure NULL, erc_result_id NULL`; `UNIQUE(plan_id, idx)` |

`copperchat.db.repos.editing_settings.EditingSettingsRepo.get(project_id) ->
EditingSettings` / `.put(project_id, **fields)`; `EditingSettings` frozen
dataclass with the four fields above. `copperchat.changes.repo.ChangeRepo`
(`add_many(entries) -> int`, `for_run(project_id, run_id, step_id=None)`,
`runs(project_id, limit)`), `ChangeEntry` dataclass mirroring the columns.

- **E1 (as built):** CHECK constraints on every enumerated column (`kind`,
  `plan_policy`, `annotation_block`, `show_agent_view`, `op`, plan and step `status`);
  `plan_steps.plan_id` references `plans` (`ON DELETE CASCADE`); indexes
  `changes(project_id, run_id)`, `erc_results(project_id, run_id)`. `change_budget`
  1–500 is validated by the repo, not the DB. `InvalidEditingSettings`
  (`copperchat.db.repos.editing_settings`, `code="invalid_argument"`, HTTP 400).
  `ChangeRepo.runs` returns `{run_id, step_ids, count, side_effects, created_at}`
  (newest run first). Test helper `tests.kicad.edit.helpers.edit_ctx(session)` builds a
  plain `ToolContext` — E2 adds its `EditingService`. The `blank` fixture has a
  `.kicad_pcb` (owner decision 4 treats it as a project with a board).

### E-C4. Two-phase mutating tools — `copperchat.tools` + `kicad_tools.edit` (E2)

```python
# copperchat.errors
class StructuredToolError(CopperChatError):
    def __init__(self, what, why, suggestion, *, code: str | None = None, details: dict | None = None)
    def to_dict(self) -> dict   # {"what","why","suggestion"} + "code"/"details" when set
class DocumentInconsistent(CopperChatError): code = "document_inconsistent"
    auto_restore: bool = False   # True: restore the last step checkpoint / turn snapshot
                                 # at turn end (owner decision 4)

# copperchat.tools.spec
PrepareFn = Callable[[ToolContext, dict[str, Any]], Awaitable["ChangeSet"]]
ApplyFn   = Callable[[ToolContext, "ChangeSet"], Awaitable[ToolResult]]   # sets cs.applied
ToolSpec.prepare: PrepareFn | None = None      # set => two-phase tool (mutating=True,
ToolSpec.apply:   ApplyFn | None = None        #   changes_documents=True, handler unused)
def edit_spec(name, description, input_schema, prepare, apply) -> ToolSpec
ToolContext.editing: EditingService | None = None
ToolContext.on_document_inconsistent: Callable[[Exception], None] | None = None
```

Adapter order for a spec with `prepare` (spec §5.1, owner rule "refusals never
snapshot"): `ctx_factory` → **read-only** (`code="read_only"`) →
`gate.before_prepare(ctx, spec.name)` → `prepare` → `gate.check_budget(ctx, cs)`
→ `on_mutation` (turn snapshot) → `apply` → `gate.record(ctx, cs)` (reads
`cs.applied`, set by `apply_change_set`) → `changed_docs`. `DocumentInconsistent` from `apply` → `ctx.on_document_inconsistent`
→ error result. Every tool call (any spec) also calls `editing.note_call(ctx, spec.name)`.

```python
# copperchat.kicad_tools.edit.changeset
@dataclass(frozen=True)
class SheetRef: path: str; page: str; file: str; name: str
# every op has `sheet: SheetRef | None = None` (None = cs.sheet); the engine writes it via
# for_sheet(op.sheet) and reads back per touched sheet (multi-sheet writes: sheet pin +
# hierarchical label, units on several instances, shared-sheet refs, labels on two sheets)
@dataclass
class Create: items: list[Any]; sheet: SheetRef | None = None   # kipy items, fresh UUIDs
@dataclass
class Update: items: list[Any]; sheet: SheetRef | None = None   # symbols: definition cleared
@dataclass
class Delete: uuids: list[str]; sheet: SheetRef | None = None
@dataclass
class Place: lib_id: str; at_mm: tuple[float, float]; orientation: int; unit: int; reference: str
             sheet: SheetRef | None = None
EditOp = Create | Update | Delete | Place
@dataclass
class Stage:                                      # one KiCad commit
    label: str
    build: Callable[[StageInput], list[EditOp]]   # sees items created/placed by earlier stages
@dataclass
class NetIntent:
    merge: list[frozenset[PinKey]] = []           # each group ends on one net
    may_change: frozenset[PinKey] = frozenset()   # pins allowed to change net (others: partition identical)
    names: dict[PinKey, str] = {}                 # expected net name per pin
    keep_partition: bool = False                  # reroute/move: partition must be identical
@dataclass
class Logical:
    changes: int                                  # budget unit (E-C6)
    deleted_symbols: list[str] = []               # UUIDs of non-power symbols deleted by this call
    placed_symbols: list[str] = []                # filled after apply (own-placement ledger)
@dataclass
class ChangeSet:
    tool: str
    sheet: SheetRef
    stages: list[Stage]
    pre_images: dict[str, Any]                    # uuid -> item copy (updated, deleted, neighbours)
    intent: NetIntent
    logical: Logical
    summary: dict[str, Any]                       # tool-specific, echoed in the result
    applied: "Applied | None" = None              # set by apply_change_set
# copperchat.kicad_tools.edit.engine
@dataclass
class Applied:
    created: list[Any]; modified: list[Any]; deleted: list[str]
    side_effects: list[ChangeEntry]; netlist: Netlist
async def apply_change_set(ctx: ToolContext, cs: ChangeSet) -> Applied
    # stages in order (one session.commit each); after the last push: read back
    # (by id + neighbourhood), verify cs.intent against live_netlist; mismatch ->
    # compensate (reverse order; deletes re-created from pre-images with original
    # UUIDs — deleted *symbols* (owner decision 4): project without .kicad_pcb ->
    # re-placed from the library (method A, new UUID; compensation returns an
    # old->new UUID map and the re-verify remaps PinKeys through it); project with a
    # .kicad_pcb -> no re-place: raise DocumentInconsistent(auto_restore=True) —,
    # updates restored, creates removed), re-verify, raise
    # StructuredToolError(code="verification_failed"); compensation failing ->
    # DocumentInconsistent. Records ChangeEntry rows (incl. side effects).
def edit_result(cs: ChangeSet, applied: Applied, **extra) -> ToolResult   # standard JSON
```

Standard mutating result: `{"created": [...], "modified": [...], "deleted": [...],
"side_effects": [...], "nets_touched": [...], "connectivity_verified": true,
"logical_changes": n, "budget": {"used", "limit"} | null, "notes": [...]}` +
tool-specific keys; every item entry carries `uuid`, `kind`, `ref?`, `sheet`,
`at_mm?`.

Error codes (`StructuredToolError.code`): `read_only`, `plan_required`,
`plan_open`, `plan_step_required`, `plan_step_exceeded`, `erc_errors_open`,
`verification_failed`, `not_found`, `ambiguous_pin`, `overlap`, `no_route`,
`libraries_loading`, `footprints_unavailable`, `invalid_argument`.

- **E2 (as built):** `StructuredToolError.code` set per instance (also the HTTP body's
  `code`); `to_dict()` adds `code`/`details` only when set. `edit_spec` builds a
  `ToolSpec` whose `handler` raises (two-phase tools only run through the adapter); the
  registry rejects a spec with only one of `prepare`/`apply` or without
  `mutating`+`changes_documents`. The adapter fills the result's `budget` key after
  `gate.record` from the new `EditGate.budget_info(ctx)`; `note_call(ctx, tool, args)`
  takes the args (`render_view` `doc`). `DocumentInconsistent` reaches the agent as an
  error with `code="document_inconsistent"`. `SheetRef` adds `human_path` (`/amp/`) and
  `as_dict()`; `ChangeSet` adds `notes`; `StageInput(created, placed, modified)` holds the
  read-back items of earlier stages; `static_stage(label, *ops)` helper. `Applied` adds
  `sheet_of`, `deleted_items`, `placed`, `nets_touched`, `item_net`, `notes`, `changes`
  (all rows written, side effects included). `apply_change_set(ctx, cs, *, _verify=None,
  _before_compensate=None)` (test seams); the engine reads missing pre-images itself
  (an unknown UUID in `Update`/`Delete` → `not_found` before any write), a KiCad
  `ApiError` while staging → `invalid_argument` (commit dropped, earlier stages
  compensated), a cancellation after a pushed stage compensates shielded, a
  `KicadUnavailable` is not compensated (the restarted server holds the last save).
  The neighbourhood = wires through and junctions at every created/moved/removed point
  of the stage; a created segment that KiCad merges at push is a result note (not a
  row). `verification_failed` details: `{expected: {merge, may_change, names,
  keep_partition}, found: [...], replaced?: {old_uuid: new_uuid}}`.
  `verify.check(before, after, intent, label=None)`, `verify.remap(netlist, {new: old})`.
  Change rows and the change log are only written when `ctx.editing` and `ctx.run_id`
  are set; `EditingService.record_changes` never fails an applied call.
  Session additions used by the engine (E-C1): `KicadSession.generation` (server starts),
  `.server_alive`, `CommitScope.pushed`. The engine never compensates against a replaced
  server (generation changed or client killed on cancellation) — it raises
  `KicadUnavailable`; compensation is shielded and survives repeated cancels; the
  adapter holds `EditingService.edit_lock(project_id)` from the gate check to the budget
  record and refuses every edit of a turn after a `DocumentInconsistent`
  (`EditTurnState.halted`, `code="document_inconsistent"`). `by_id` bisects only on
  `AS_BAD_REQUEST` (KiCad's answer for unknown ids). `sch_add_wire` takes at most 64
  points and refuses ends on unjoined wire crossings or touching several nets.
- **E7 (as built):** `kicad_tools/edit/sheets.py` (`validate_sheet_file(project, file, *,
  existing)`, `validate_sheet_name`, `validate_pin_name`, planners) + `specs_sheets.py`.
  `sch_add_sheet(name, file, at, size?, parent?)` **never accepts an existing file**
  (`invalid_argument`, tool passes `existing=set()` after refusing hierarchy files with a
  tailored message): found against KiCad at the pinned commit that a sheet symbol created
  through the API for an *existing* file gets a blank screen and `save()` **replaces the file**
  (`ampli_ht.kicad_sch` became an 8-line stub), so a second instance of a sheet cannot be made
  by the API (do it in KiCad). Also refused: the root file, a file of an ancestor, a name
  already used by a sibling, a parent sheet living in a sub-directory, an occupied area
  (`overlap`: symbols, sheets, wires, labels, junctions, no-connects +2.54 mm). Stages: Create
  with the absolute path, Update to the relative `Sheetfile`; the tool then calls
  `session.save("sch")` itself (the file exists, hash listener runs) and records its change
  rows only **after** that save (`apply_change_set(..., record=False)`, new keyword; the
  rows still carry the call's plan step and `call_seq`; apply re-checks that the file still does
  not exist). A failing/cancelled
  save: sheet symbol removed in a commit, the new file (and a directory this call made)
  deleted, root saved again (`DocumentInconsistent` if that undo fails); the agent gets
  `invalid_argument`. Result `sheet: {name, file, path, page, uuid}`. `sch_add_sheet_pin(sheet,
  name, direction, side?)`: `sheet` is the **sub-sheet instance**; its sheet symbol is the
  last component of `SheetRef.path`, its parent the rest. Pin on the left (outputs: right)
  edge, 2.54 mm stack on the 1.27 grid, label at a free spot on the left of the sub-sheet;
  on a shared sheet file an existing hierarchical label of that name is reused (`label:
  null` + note, verified with `hier_session`). Result `pin: {sheet, name, at_mm, side,
  direction, uuid}`, `label: {sheet, at_mm, text, uuid} | null`. **Gap for E5/E6:**
  `sch_add_wire`'s `NetIntent` does not know that a sheet pin joins the nets of the
  sub-sheet; a wire onto a pin whose sub-sheet net already holds symbol pins fails with
  `verification_failed` (R1.1 "now also connected" to R201.1). `sch_connect` must treat sheet
  pins as endpoints and put the cross-sheet pins into `intent.merge`.
  `tests.support.edit.call_tool` takes `name` positional-only (tools have a `name` argument).

### E-C5. Editing state and gate — `copperchat.editing` (E2; E8b supplies `PlanGate`)

```python
@dataclass
class EditTurnState:                    # one per (project_id, run_id)
    logical_used: int = 0
    deleted_preexisting: int = 0
    placed_this_turn: set[str]          # symbol UUIDs placed in this turn
    call_seq: int = 0                   # every tool call
    last_mutation_seq: int | None
    last_erc_seq: int | None; last_erc: list[dict] | None   # E8a: no double runs
    last_render_seq: int | None         # render_view(doc="sch")
    active_step_id: str | None          # E8b
class EditGate(Protocol):
    async def before_prepare(self, ctx: ToolContext, tool: str) -> None
    async def check_budget(self, ctx: ToolContext, cs: ChangeSet) -> None
    async def record(self, ctx: ToolContext, cs: ChangeSet) -> None   # cs.applied is set
class BudgetGate(EditGate)              # E2: settings policy + turn budget (no plans)
class EditingService:
    def __init__(self, *, db, broadcaster, settings_repo, gate: EditGate | None = None)
    gate: EditGate
    changes: ChangeRepo
    def turn(self, project_id: str, run_id: str | None) -> EditTurnState
    def note_call(self, ctx: ToolContext, tool: str) -> None
    on_changes_recorded: list[Callable[[str, str, str | None, int], Awaitable[None]]]
def install_editing(app) -> EditingService   # lifespan, after install_history, before resume_pending;
                                             # sets runtime.editing; wraps runtime.hooks_factory in ChainedHooks
```

`copperchat.agent.engine.ChainedHooks(hooks: list[RunHooks])` calls each hook in
order (history first). `copperchat.editing.hooks.EditingHooks(RunHooks)` is the
editing hook (E8a: turn-end ERC delta; E8b: plan pause/turn bookkeeping).
`TurnOutcome.document_inconsistent: bool = False` (E2).

- **E2 (as built):** `TurnOutcome.auto_restore`; `RunEngine.editing` / `AgentRuntime.editing`
  (set by `install_editing`, passed into each `ToolContext` with
  `on_document_inconsistent`). `ChainedHooks`: start/turn-start/first-mutation hooks stop
  at the first error (a failed snapshot fails the call); every turn-end hook runs, then
  the first error propagates. `EditingService.drop_turn`, `.record_changes(ctx, entries)`;
  `EditGate.budget_info(ctx) -> {used, limit} | None` (`None` under policy `never`).
  `document_inconsistent` turns: the engine posts one error row (code
  `document_inconsistent`); `HistoryHooks` never autosaves, reopens the session once
  (or, with `auto_restore`, restores the turn snapshot via
  `restore_snapshot(..., check_busy=False, notice=...)`, notice action `restored`) and
  posts "The design is at the state of the last save (<time>); changes after that are
  lost." (`restore` action; `recovered` when no snapshot exists) — the same wording for
  `kicad_failed` turns that mutated. `KicadSessionManager.reopen` keeps `last_saved_at`.

### E-C6. Logical change unit (E2 counts, E8b budgets)

| Change | Logical |
|---|---|
| placed symbol (all units, fields set at placement) | 1 |
| field edit on one reference (all units, any number of fields) | 1 |
| moved / rotated / mirrored symbol or label (incl. dragged wires) | 1 |
| deleted symbol / label / sheet (incl. attached stubs, power, no-connects) | 1 |
| connection (`sch_connect`, `sch_add_wire`, reroute: all its segments + junctions) | 1 |
| pin given a label / power symbol / no-connect | 1 per pin |
| renamed net | 1 |
| new sheet / new sheet pin (incl. its hierarchical label) | 1 each |
| annotation change | 1 per changed reference |
| duplicate | sum of the copied items by the rules above |

Budget refusal: `used + cs.logical.changes > limit` or `deleted_preexisting +
|deleted_symbols ∖ placed_this_turn| > 3`. Power symbols (`#PWR`/`#FLG`, also
when deleted as attached items) never count toward the ≤ 3 limit. Deleting a symbol from
`placed_this_turn` refunds its placement (`logical_used -= 1`) and costs 0.
`plan_policy = never` → no budget; `always` → `plan_step_required` outside a step.

### E-C7. Geometry and scene — `kicad_tools.edit.geometry`, `.scene` (E2)

```python
GRID_MM = 1.27
def snap(v: float) -> float; def on_grid(p) -> bool
@dataclass(frozen=True)
class Box: x0: float; y0: float; x1: float; y1: float   # mm, y down
def canonical_transform(rotation: int, mirror: Literal["", "x", "y"]) -> tuple[int, bool, bool]
    # the 8 distinct transforms as (orientation, mirror_x, mirror_y); 180+x is sent as 0+y (2A Q1)
@dataclass(frozen=True)
class PinEnd: key: PinKey; ref: str; number: str; name: str; type: str
              at: tuple[float, float]; direction: Literal["left","right","up","down"]  # outward
def pin_ends(sym, unit_only: bool = True) -> list[PinEnd]      # port of spikes/2a geom.pin_ends_abs
def body_box(sym) -> Box                                        # library graphics + pins, +2 mm margin
def field_boxes(sym) -> list[Box]                               # visible fields (get_text_extents)
@dataclass
class Scene:                     # one sheet instance, read via for_sheet
    sheet: SheetRef
    symbols: dict[str, SymbolInfo]; wires: dict[str, WireInfo]; labels: dict[str, LabelInfo]
    junctions: dict[str, tuple[float,float]]; no_connects: dict[str, tuple[float,float]]
    def obstacles(self, exclude: set[str] = ...) -> list[Box]
    def foreign_pin_ends(self, allowed_nets: set[str], netlist: Netlist) -> list[PinEnd]
def read_scene(h: KicadHandle, sheet: SheetRef) -> Scene
def resolve_sheet(h: KicadHandle, sheet: str | None) -> SheetRef   # name, path or page; default root
def resolve_pin(h, spec: str) -> PinEnd     # "U1.3" / "U1.VIN" over all sheets; not_found / ambiguous_pin
def all_symbols(h) -> list[SymbolInfo]      # every sheet instance (ref, uuid, sheet path, page, unit, lib_id)
```

- **E2 (as built):** `canonical_transform` returns degrees (`DEG_SSO`/`SSO_DEG` map to
  kiapi); extras `EPS_MM`, `snap_point`, `same_point`, `mm_point`, `transform_matrix`,
  `CANONICAL`, `symbol_transform`, `symbol_fields`, `on_segment`,
  `in_segment_interior`, `orthogonal`; `Box.around/grow/contains/overlaps/
  hit_by_segment/as_dict`. `body_box(sym, margin=2.0)`; `SymbolInfo.body` has no margin,
  `Scene.obstacles` adds 2 mm. `field_boxes(sym, extents=None)` estimates 0.6 × size per
  character (one IPC call per field for `get_text_extents` was too slow for a scene;
  `extents` plugs it in). `Scene` adds `sheets` (sheet-symbol boxes), `all_pins`,
  `items_at`, `wires_through`; module helpers `sheet_refs`, `sheet_ref`, `sheet_api`
  (`for_sheet` by `SheetRef`), `symbol_info`, `item_nets(h)` (the live netlist's members
  include wires, junctions and labels, so items map to nets), `resolve_pin(h, spec,
  symbols=None)` (number, then exact, then case-insensitive name). The stock library has
  no `Device:Q_NPN_BCE` at the pinned commit; tests use `Device:Q_NPN`.

### E-C8. Annotation — `kicad_tools.edit.annotate` (E2)

```python
class Allocator:
    @classmethod
    def from_design(cls, h: KicadHandle, block: Literal[100, 1000]) -> "Allocator"
    scheme: Literal["per_sheet", "flat"]     # detected: existing refs outside the page
                                             # blocks => "flat" (existing scheme wins)
    def next_ref(self, prefix: str, page: str) -> str    # reserves it; R{n}01..R{n}99 /
        # R{n}001..; single-sheet projects R1, R2..; raises StructuredToolError("block full")
    def next_power(self, prefix: Literal["#PWR", "#FLG"]) -> str   # "#PWR01" style, unique
```

- **E2 (as built):** plain-data core `Allocator(symbols: Iterable[(ref, page)], pages,
  block)`; `from_design` reads `sheet_refs` + `get_symbols`. A single-sheet project is
  `flat` (max + 1 from 1); per-sheet fills the lowest free number of the block; a ref
  counts as "in its page block" for either block size (switching 100 → 1000 keeps a
  per-sheet design per-sheet); non-numeric pages number flat; block full →
  `invalid_argument` naming the 1000 block and `sch_annotate mode=renumber`;
  `next_power` takes the lowest unused number (≥ 2 digits).

### E-C9. Library — `copperchat.library` (E3)

```python
def library_for(session: KicadSession) -> LibraryService    # one per session (weak map)
class LibraryService:
    state: Literal["loading", "ready", "failed"]
    async def start(self) -> None                      # background warm-up (idempotent)
    async def search_symbols(self, query: str, library: str | None = None, limit: int = 20) -> list[dict]
    async def get_symbol(self, lib_id: str) -> SymbolDetail    # pins incl. derived (probe)
    async def search_footprints(self, query: str, library: str | None = None, limit: int = 20) -> list[dict]
    async def footprint(self, lib_id: str) -> FootprintDetail | None   # lazy library load
    def footprints_available(self) -> bool             # board open
@dataclass(frozen=True)
class SymbolDetail: lib_id; description; keywords; footprint; footprint_filters; datasheet
    unit_count: int; body_styles: int; is_power: bool; pins: list[LibPin]
    body_size_mm: tuple[float, float]
    pin_directions_by_rotation: dict[str, dict[str, str]] | None   # ≤ 4 pins: {"0": {"1":"left",..}, ...}
@dataclass(frozen=True)
class FootprintDetail: lib_id; description; pad_count: int; smd_pads: int; package: str | None
def package_of(footprint_name: str) -> str | None     # port of spikes/2b libdata.package_of
@dataclass(frozen=True)
class PartPreferences: passives_package: str | None; preferred_libraries: list[str]; raw: dict[str, str]
def effective_preferences(settings, project) -> PartPreferences
def preference_notes(prefs, lib_id: str, footprint_name: str, footprint: FootprintDetail | None) -> list[dict]
    # [{"kind": "preference_deviation", ...}] for passives (Device:R/C/L/FerriteBead*) whose
    # package differs: package_of(footprint_name); the 2-SMD-pad cross-check only when
    # `footprint` is known (it is None without a board, owner decision 3); unknown -> no note
def active_part_note(project, ref: str, fields: dict) -> dict | None
    # {"kind": "active_part_without_notes"} for U/Q/IC refs without MPN or without notes.md
```

- **E3 (as built):** extra module `library/symbols.py` (proto → `SymbolRow` / `LibPin` /
  `SymbolDetail`, body size, pin directions). The warm-up keeps only the search rows
  (`index.SymbolRow`: `lib_id, description, keywords, unit_count, pin_count, footprint,
  footprint_filters, scope`), not the protos: `get_symbol` re-fetches one symbol
  (`GetItemsFromLibrary`, ~11 ms) and caches the detail (LRU 512); a derived symbol is probed
  once (`session.commit(dry_run=True)` + `place_symbol_from_library`), which also fills
  `pin_count` in later search rows. Because the probe opens a commit, **`get_symbol` must not
  be called inside an open commit** (E4: call it in `prepare`); a **read-only** session does not
  probe (`pins_known=False`, `pins=[]`). `LibPin(number, name, type, unit, body_style, at_mm,
  direction)` (direction = outward at rotation 0); `SymbolDetail` adds `reference_prefix`,
  `value`, `pins_known` and `to_dict()`; `pin_directions_by_rotation` is computed from the
  library pin orientations (no probe per rotation), keys = pin numbers, ≤ 4 pins, unit 1.
  `LibraryService` adds `error`, `wait_ready(timeout) -> bool` (the `lib_*` tools wait up to
  `specs.READY_WAIT_S` = 30 s, then `libraries_loading`), `footprint_libraries()` (nicknames →
  scope, no load), `suggest_footprint_libraries(query)`, `loaded_footprint_libraries()`.
  `footprint()` returns `None` for an unknown id and raises `footprints_unavailable` without a
  board (callers check `footprints_available()` first); `search_*` raise `not_found` for an
  unknown `library` and `invalid_argument` for an empty or > 200-character query or a
  non-integer `limit` (≤ 50). KiCad refusals inside the service become `StructuredToolError`
  (`library_error`, new code; `LibrariesLoading` → `libraries_loading`); a footprint search
  without `library` skips a library that fails to load and remembers it (not retried by
  unscoped searches; an explicit `library` retries): `search_footprints_detailed()` returns
  `(rows, failed)`, tool key `failed_libraries`. Index searches run in a worker thread; library
  free text is clipped (500 chars). `started` tells whether a warm-up ran. `parts_get_notes`
  reads at most 64 KiB of notes (`truncated: true`); `lib_get_symbol` validates `lib_id`, takes an
  optional `unit` (pins of that unit + common pins) and returns at most 256 pins
  (`pins_truncated: true`, `pin_count` stays the full count). A failed footprint library is
  skipped by unscoped searches for 5 min (`FAILED_RETRY_S`) and does not take a top-3 slot.
  `FootprintDetail` adds `keywords`, `to_dict()`. `PartPreferences` adds
  `preferred_manufacturers`, `assembly`; helpers `parse_preferences(text)`,
  `preferences_from_raw(raw)`, `is_passive(lib_id)` (Device:R/C/L incl. `_Small`/`_US`,
  `FerriteBead*`). A deviation note also carries `preference`, `expected`, `actual`,
  `footprint`, `message` and `suggested_footprint`. `active_part_note` reads the MPN from a
  field named `MPN` / `Manufacturer Part Number` (case-insensitive); notes carry `reason`
  (`no_mpn` | `not_researched` | `invalid_mpn`). `seed_user_part_preferences(settings)` runs in
  the app lifespan: it customises the built-in (origin sidecar kept) with `passives_package:
  0805` once (marker `<data_dir>/skills/.part-preferences-seeded`), never touching an existing
  user copy. `skills.store.layer_contents(layers, name)` returns the valid SKILL.md per layer.
  The `part-preferences` skill reaches the main agent like any skill. `StructuredToolError`
  already gained E-C4's `code`/`details` keywords in E3 (identical signature to E2's).

- **E4 (as built, place/fields/space/annotate):** `space.find_space(scene, shape, near, *,
  direction=None, keep_free=(), outline=None, limit=3) -> list[Candidate(at, box, distance)]`
  takes the part's boxes already transformed and relative to its origin (the caller lays the
  part out), not `(size_box, rotation)`. `sch_place_symbol`'s overlap check (bodies + 2 mm,
  fields, sheet symbols, a conservative cross per label, wires, junctions, no-connects, new
  pins on any connection point) is a subset of `find_space`'s (adds 1.27 mm body clearance,
  the part's own pin corridors and the corridor of every unconnected pin), so every candidate
  is placeable; no upper page bound (only 10 mm from the top-left corner). Both probe the part
  in a dry-run commit (`place.probe_symbol`), so `sch_find_space` refuses a read-only session
  (`read_only`) and lays out with the reference a placement would get. Placement stage 2 sets
  `cs.intent.may_change` from the placed pins inside its `build` (the UUID is only known
  then). Shared sheet: the other instances' references are a separate change set (`Logical(0)`)
  after `session.refresh_jobs()` (save + close/reopen = 2A's "save + revert"); if it fails the
  placement is removed in a plain commit (`DocumentInconsistent` if that fails too); result
  `placed.instances`. `reference` is only accepted for another unit of an existing multi-unit
  part (same lib_id and sheet instance, unit free): the unit inherits the sibling's fields and
  flags, given fields are written to the siblings too (`modified`). `place.ToolChangeSet`
  (ChangeSet + `plan`, `extra_notes`) carries structured notes: result `notes` mixes strings
  and `{kind: footprint_unverified | footprint_not_found | no_footprint |
  preference_deviation | active_part_without_notes, ...}`; an explicit unknown footprint is
  `not_found` with `details.closest`. Fields are always centred (H and V) so KiCad's
  justification transform cannot move them; angle 90 on 90/270 parts. `sch_set_fields`
  refuses a power symbol's Value (`sch_rename_net`), removing standard fields and no-ops;
  result `changed: {field: {before, after}}`, `units`. `sch_annotate` (planning in
  `edit/annotate_tool.py`) defaults to `scope="new"` (unannotated + placed this turn),
  `mode="keep"` (unannotated and duplicates); reads units per sheet instance via `for_sheet`;
  a call with nothing to rename is a no-op (`logical_changes: 0`); `renamed` lists at most 200
  entries (`renamed_count`, `truncated`); `mode="renumber"` keeps the design's scheme by majority
  (`annotate_tool.detect_scheme`: per-sheet when most refs sit in their page block), `keep` uses
  the allocator's rule like `sch_place_symbol`. `sch_find_space` takes an optional `value`; an
  explicit `reference` no unit uses is refused; layout/search run in `asyncio.to_thread` after a
  read-only `session.call`; probed parts are cached per session generation. Known limit: one UUID on
  two instances of a shared sheet is compensated for its first instance only (the engine keys
  pre-images by UUID). Test helper `tests.support.kicad_cli.cli_components`.

### E-C10. Checks, checkpoints, changes — `copperchat.changes`, `history` (E8a)

```python
HistoryStore.snapshot(run_id, label, preview_png=None, *, kind="turn", plan_step_id=None) -> Snapshot
Snapshot.kind: str; Snapshot.plan_step_id: str | None
HistoryStore.latest_for_run(run_id)          # kind = turn only (unchanged meaning)
HistoryStore.latest_step_checkpoint(run_id) -> Snapshot | None
async def take_step_checkpoint(svc: HistoryService, project, run_id, step_id, label) -> Snapshot
    # session.save("sch") (listener records hashes) -> snapshot(kind="step")
def violation_id(v: dict) -> str             # sha1(type + sorted item uuids)[:12]
@dataclass
class ErcDelta: new_errors: list[dict]; new_warnings: list[dict]; resolved: list[dict]
async def erc_baseline(svc, project, snapshot: Snapshot) -> list[dict]   # cached <snapdir>/erc.json
async def current_erc(ctx_or_session, state: EditTurnState | None) -> list[dict]
    # reuses state.last_erc when last_erc_seq > last_mutation_seq
def erc_delta(baseline, current) -> ErcDelta   # match by violation_id, position tie-break
async def diff_against_snapshot(svc, project, snapshot) -> dict          # kicad-cli sch diff JSON,
    # compacted: {"sheets": {sheet_path: [{"id","kind","type","uuid","ref","box_mm"}]}}
```

- **E8a (as built):** `take_step_checkpoint` lives in `copperchat.changes.checkpoints`; it
  holds `HistoryService.lock(project)` (callers must not), saves only when the project has a
  schematic, renders a preview like a turn snapshot, and raises `KicadUnavailable` /
  `ReadOnlyProject` without an open writable session. The manifest also records `kind` /
  `planStepId`. Pruning: `HISTORY_KEEP` turn snapshots; step checkpoints of a pruned turn's
  run go with it, and so do step checkpoints of other runs older than the oldest kept turn
  snapshot. Recovery: after the turn-snapshot `restore` notice, a second notice
  `{action: "restore", snapshotId: <last step checkpoint>, checkpoint: true}` when the run has
  one; `auto_restore` restores the run's last step checkpoint, else the turn snapshot (notice
  "…restored to its state at the end of the last finished plan step"). ERC items name **pins
  by the pin's own KIID** (the placed symbol's `definition.pins[].id`, E-C2), not the symbol
  UUID, so `violation_id` of a `pin_not_connected` hashes pin KIIDs (stable across moves and
  saves). `erc_delta` counts only `error`/`warning` severities (exclusions/info never "new"),
  entries gain `id`; ties are broken by distance (another sheet instance always farther).
  `erc_baseline` copies the snapshot via `history.restore.snapshot_plan` (manifest + hash
  checked, formerly `_plan`) plus the project's `sym-lib-table`/`fp-lib-table`, one run per
  snapshot at a time, cache written atomically; it needs the project open (settings of the
  session). `current_erc(session, state)` records its report into the state
  (`last_erc`, `last_erc_seq = call_seq`); `erc_is_fresh(state)` is the reuse rule.
  `checks.run_kicad_check_on_dir(settings, project, doc_path, doc, *, parity=False)`,
  `checks.parse_report(raw, doc)`, `checks.PROJECT_SUPPORT_FILES` (was private).
  `ErcResultRepo` (`changes.erc`: `add(...) -> id` (uuid4 hex), `get(project_id, id)`
  (400 `invalid_argument` / 404 `not_found`), `latest_for_runs`); `EditingService` gains
  `erc_results`, `history` (set by `install_editing` from `app.state.history`) and
  `peek_turn`. Turn-end ERC (`EditingHooks`): only when `sch` ∈ `changed_docs`, the turn's
  state has a `last_mutation_seq` (a two-phase tool applied) and neither `kicad_failed` nor
  `document_inconsistent`, the session is open and writable and the run has a turn
  snapshot; the row carries `plan_step_id = state.active_step_id`; failures post a notice
  `{action: "erc-failed"}` (never an error row, never raises); new errors also become an
  agent note for the next turn. Diff (`changes.diff`): `run_sch_diff` (raw JSON, exit 0/5)
  + `compact_diff(raw, log)`; entries add `properties` (added/removed: `[{name}]`,
  modified: `{name, before, after}`) and `side_effect` (no own, non-side-effect log row
  of that kind for the UUID; always false without a log); result adds `count`. `ChangeRepo`
  adds `since(project_id, created_at)` and `for_steps(project_id, step_ids)`.
  `sch_changes` (`copperchat.changes.specs`, read-only): result `{scope, count,
  side_effects, changes: [{op, kind, uuid, sheet_path, tool, call_seq, ref?, net?,
  plan_step_id?, before?, after?, side_effect?}], truncated?}` (newest 200); `step` needs
  `active_step_id`, `plan` reads the open plan's steps from `plan_steps`; no scope data →
  `not_found`.

### E-C11. Plans — `copperchat.plans` (E8b)

```python
PlanStatus = Literal["proposed","approved","running","paused","done","rejected","aborted"]
OPEN = {"proposed","approved","running","paused"}
class PlanService:
    async def propose(ctx, title, summary, steps) -> Plan
    async def revise(ctx, steps, reason) -> Plan
    async def approve / reject / resume / abort(project, plan_id) -> Plan        # REST
    async def rollback(project, plan_id, to_step: int) -> Plan                  # REST, 409 agent_busy
    async def step_begin(ctx, idx) -> PlanStep; async def step_done(ctx, idx, summary, justifications) -> dict
    async def pause(project, reason, step_status=None) -> None
    async def pause_all_running() -> None      # startup
class PlanGate(BudgetGate)                     # replaces BudgetGate in EditingService.gate
```

`PlanDTO` (camelCase on the wire): `{id, projectId, title, summary, status,
revision, pauseReason, steps: [{idx, title, intent, expectedChanges, touches,
verification, status, changeCount, checkpointId, summary, failure, erc:
{errors, warnings, justifications} | null}], createdAt, updatedAt}`.

### E-C12. HTTP and WS additions

- `GET|PUT /projects/{id}/settings/editing` → `{planPolicy, changeBudget, annotationBlock, showAgentView}` [E1]
- `GET /projects/{id}/history` entries gain `kind`, `planStepId` [E8a]
- `GET /projects/{id}/changes/runs?limit=20` → `[{runId, stepIds, count, sideEffects, erc: {errors, warnings, resultId} | null, snapshotId, createdAt}]` [E8a]
- `GET /projects/{id}/changes?runId=&stepId=` → `[ChangeEntry camelCase]` [E8a]
- `GET /projects/{id}/changes/diff?snapshotId=` → E-C10 diff shape [E8a]
- `GET /projects/{id}/erc-results/{resultId}` → `{newErrors, newWarnings, resolved, justifications}` [E8a]
- `GET /projects/{id}/plans/open` → `PlanDTO | null`; `GET /projects/{id}/plans/{planId}`;
  `POST /projects/{id}/plans/{planId}/approve|reject|resume|abort`; `POST …/rollback {toStep}` [E8b]
- WS S→C `changes-updated{projectId, runId, stepId: str|null, count}` [E8a];
  `plan-updated{projectId, plan: PlanDTO}` [E8b]. Each phase adds the pydantic
  frame, the zod mirror in `frames.ts` (+ `ALL_FRAME_TYPES`) and
  `protocol-fixtures/<type>.json`.
- Notice metadata actions: `erc-delta` `{ercResultId, errors, warnings}` [E8a];
  `plan` `{planId}` (posted by `plan_propose`, renders the plan card) [E8b];
  `restore` notices may carry `checkpoint: true` [E8a].

- **E8a (as built):** `/changes/runs` takes `limit` 1–100 (422 outside); `erc` is the run's
  newest ERC result (turn or step), `snapshotId` its turn snapshot (or null). `/changes`
  needs `runId`; `runId`/`stepId` must match `[A-Za-z0-9_-]{1,100}` (400
  `invalid_argument`). `/changes/diff` → `{snapshotId, count, sheets: {sheetPath: [{id,
  kind, type, uuid, ref, boxMm, properties, sideEffect}]}}` (camelCase), log = the change
  rows since the snapshot's `created_at`; 400 invalid / 404 unknown snapshot, 503 when the
  project is not open. `/erc-results/{id}` also returns `id, runId, planStepId,
  baselineSnapshotId, createdAt`. Extra notice action `erc-failed` (turn-end ERC could not
  run). `changes-updated.runId` is non-empty; the chat store ignores the frame (E9a's
  Changes panel consumes it).

### E-C13. Frontend (E9a/E9b)

- KiCanvas fork (`copperchat` branch) adds to the schematic viewer:
  `highlight(targets: string[], kind: "added"|"modified"|"removed"|"focus") -> {found, missing}`,
  `highlight_boxes(boxes: {x,y,w,h,kind}[])` (mm), `clear_highlight()`; own overlay
  layer, theme-aware colours, reset in `load()` (2C follow-up 2). `KcViewer` in
  `@/lib/kicanvas` gains these optional methods.
- `@/stores/view` adds `highlight: {refs: string[], boxes: Box[], sheetPath: string|null, seq: number} | null`,
  `setHighlight(h)`, `clearHighlight()`, `find`/`setFind` (moved from `Viewer` local state);
  `KicadPaneProps.highlight?`.
- `@/stores/changes` (`createChangesStore(ws)`; `runs`, `entries(runId, stepId?)`, refresh on
  `changes-updated`); panel id `changes` (order 15).
- `@/stores/plans` (`open: PlanDTO | null`, `load(projectId)`, actions → REST, `plan-updated`);
  `PlanCard({planId})` rendered for notice rows with `metadata.action === 'plan'`.
- `HistoryEntry` gains `kind`, `planStepId`; `EditingSection` in Settings (current project).

---

## Decided by owner (2026-10-18)

Binding; the spec was amended in the plan PR (§3, §7.1, §13, §15).

1. **Library memory:** all **symbol** libraries are loaded eagerly
   (~1.5 GB RSS); **footprint** libraries lazily, one library per request (spike
   2B measured 4.9 GB for everything).
2. **Change budget counts logical changes** (placed part, connection,
   label/power symbol per pin, field edit, move, delete; a part the agent placed
   and deletes again in the same turn does not count); limit **20 per turn**;
   ≤ 3 symbol deletions unchanged (counted for symbols that existed before the
   turn).
3. **Projects without a `.kicad_pcb`:** footprint **existence** is not checked
   (no load of all footprint libraries); `sch_place_symbol` / `sch_set_fields`
   mark the footprint `unverified` in the result and the agent tells the user
   (skill rule). The package/preference check (e.g. 0805) still runs from the
   footprint name. Footprint search tools return `footprints_unavailable`.
4. **Compensating a failed symbol delete:** project **without** a board → the
   symbol is re-placed from the library (fields, unit, transform, body style
   copied; new UUID, lossless); project **with** a `.kicad_pcb` → no re-place: the
   tool raises `DocumentInconsistent(auto_restore=True)` and the turn end restores
   the last step checkpoint of the run, else the turn snapshot, so the board link
   is never silently broken.

## Calls made by the plan author (from the spike results)

- **Placement and new sheets use two commits in one tool call** (KiCad ignored
  an update to a sheet staged in the same commit, 2A Q7; the spikes always placed
  symbols first and updated them in a second commit): stage 1 places/creates,
  stage 2 sets fields/transform (resp. resets `Sheetfile` to relative); a failure
  in stage 2 or in verification compensates stage 1. One tool call stays one
  logical, verified, all-or-nothing change.
- **Re-creating a deleted symbol from its pre-image is never used** (lossy
  whenever a sibling instance exists — `:R_1` cache entry — or the symbol has two
  body styles, 2A Q6); see owner decision 4 for what happens instead. Non-symbol
  items always keep their UUIDs.
- **Change log + `kicad-cli sch diff`**: the log is recorded by the tools from
  read-back; the diff (JSON only) feeds the viewer's diff layer and the tests'
  cross-check; diff entries without a log counterpart are shown as side effects.
- Settings › Editing lives in the global Settings page and edits the **current
  project** (the page has no project route).

## Provisional — needs owner confirmation

None — both provisional items were decided by the owner on 2026-10-18 (decisions
3 and 4 above).

---

## Phase Conventions (apply to every phase)

- **Branch/PR:** one PR per phase, title `editing phase <E#>: <short goal>`, via
  `gh` CLI (GitHub MCP is broken). Commits end with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, PR bodies with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- **TDD:** failing test → see it fail → minimal code → pass → commit, per task.
- **Commands:** as the foundation (`uv run pytest -m "not kicad"`, ruff, mypy;
  `uv run pytest -m kicad` inside `ghcr.io/peterus/copperchat-kicad-base:<KICAD_COMMIT>`
  — the spikes' `run.sh` shows the docker invocation; frontend `npm run typecheck
  && npm run lint && npm test -- --run && npm run build`).
- **Real-KiCad tests** live in `backend/tests/kicad/edit/`, use fixtures
  `blank_session` / `ecc83_session` / `hier_session` (E1 adds `blank_session` and
  a `edit_ctx(session)` helper building a `ToolContext` with an `EditingService`),
  and assert connectivity with `live_netlist` **and** at least once per phase with
  `kicad-cli sch export netlist` on a saved copy. Never mock KiCad in tool tests.
- **Review:** after CI is green, `kontor-recipes:review-loop --max-rounds 2
  --dimensions bugs+tests,security+performance`, a fresh reviewer child per round.
  **Never Copilot.**
- **Done report:** `kontor_send_to_session(<coordinator>, status:'done')` with PR,
  CI, review result and deviations; contract deviations are reflected in this
  index in the same PR, plus an "as built" bullet under the contract.
- **No placeholders** in merged code; stubs only where this plan names a later owner.
