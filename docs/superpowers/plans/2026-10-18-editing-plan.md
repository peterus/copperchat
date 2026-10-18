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
    def add_save_listener(self, fn: Callable[[Project], Awaitable[None]]) -> None
```

`HistoryService` registers its hash recording as a save listener (E1), so
**every** CopperChat save records hashes right after it.

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

### E-C3. Database — `0006_editing.sql` (E1, used by all)

| Table / change | Columns |
|---|---|
| `history_snapshots` + | `kind TEXT NOT NULL DEFAULT 'turn'` (`turn`\|`step`), `plan_step_id TEXT NULL` |
| `editing_settings` | `project_id TEXT PK, plan_policy TEXT DEFAULT 'large'` (`always`\|`large`\|`never`), `change_budget INTEGER DEFAULT 20, annotation_block INTEGER DEFAULT 100` (100\|1000), `show_agent_view INTEGER DEFAULT 1` |
| `changes` | `id INTEGER PK, project_id, run_id, plan_step_id NULL, call_seq INTEGER, tool TEXT, op TEXT` (`add`\|`modify`\|`delete`), `kind TEXT, uuid TEXT, sheet_path TEXT, ref NULL, net NULL, before TEXT NULL, after TEXT NULL` (JSON), `side_effect INTEGER DEFAULT 0, created_at` |
| `erc_results` | `id TEXT PK, project_id, run_id, plan_step_id NULL, baseline_snapshot_id TEXT, new_errors TEXT, new_warnings TEXT, resolved TEXT, justifications TEXT` (JSON), `created_at` |
| `plans` | `id TEXT PK, project_id, title, summary, status, revision INTEGER, pause_reason NULL, base_snapshot_id NULL, created_run_id, created_at, updated_at`; `UNIQUE INDEX ux_plans_open ON plans(project_id) WHERE status IN ('proposed','approved','running','paused')` |
| `plan_steps` | `id TEXT PK, plan_id, idx INTEGER, title, intent, expected_changes INTEGER, touches TEXT (JSON {refs,nets}), verification TEXT, status` (`pending`\|`running`\|`done`\|`failed`\|`interrupted`), `run_id NULL, checkpoint_id NULL, change_count INTEGER DEFAULT 0, summary NULL, failure NULL, erc_result_id NULL`; `UNIQUE(plan_id, idx)` |

`copperchat.db.repos.editing_settings.EditingSettingsRepo.get(project_id) ->
EditingSettings` / `.put(project_id, **fields)`; `EditingSettings` frozen
dataclass with the four fields above. `copperchat.changes.repo.ChangeRepo`
(`add_many(entries) -> int`, `for_run(project_id, run_id, step_id=None)`,
`runs(project_id, limit)`), `ChangeEntry` dataclass mirroring the columns.

### E-C4. Two-phase mutating tools — `copperchat.tools` + `kicad_tools.edit` (E2)

```python
# copperchat.errors
class StructuredToolError(CopperChatError):
    def __init__(self, what, why, suggestion, *, code: str | None = None, details: dict | None = None)
    def to_dict(self) -> dict   # {"what","why","suggestion"} + "code"/"details" when set
class DocumentInconsistent(CopperChatError): code = "document_inconsistent"

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
    # UUIDs — deleted *symbols* re-placed from the library (method A, new UUID, see
    # author calls; compensation returns an old->new UUID map and the re-verify
    # remaps PinKeys through it) —, updates restored, creates removed), re-verify, raise
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
def preference_notes(prefs, lib_id: str, footprint: FootprintDetail | None) -> list[dict]
    # [{"kind": "preference_deviation", ...}] for passives (Device:R/C/L/FerriteBead*) whose
    # package (known, 2 SMD pads) differs; unknown package -> no note
def active_part_note(project, ref: str, fields: dict) -> dict | None
    # {"kind": "active_part_without_notes"} for U/Q/IC refs without MPN or without notes.md
```

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

## Calls made by the plan author (from the spike results)

- **Placement and new sheets use two commits in one tool call** (KiCad ignored
  an update to a sheet staged in the same commit, 2A Q7; the spikes always placed
  symbols first and updated them in a second commit): stage 1 places/creates,
  stage 2 sets fields/transform (resp. resets `Sheetfile` to relative); a failure
  in stage 2 or in verification compensates stage 1. One tool call stays one
  logical, verified, all-or-nothing change.
- **Compensating a deleted symbol** re-places it from the library with its
  fields, unit, transform and body style copied (2A Q10 method A — lossless and
  KiCanvas-safe) and therefore a **new UUID**; the error result names the old
  and new UUID. Re-creating from the pre-image is not used: it is lossy whenever
  a sibling instance exists (`:R_1` cache entry) or the symbol has two body
  styles (2A Q6). Non-symbol items always keep their UUIDs. This path only runs
  when verification after a delete fails.
- **Change log + `kicad-cli sch diff`**: the log is recorded by the tools from
  read-back; the diff (JSON only) feeds the viewer's diff layer and the tests'
  cross-check; diff entries without a log counterpart are shown as side effects.
- **Footprint search without an open board** (project has no `.kicad_pcb`):
  footprint tools return `footprints_unavailable`; `sch_place_symbol` then records
  the footprint as `unverified` in its notes (never refused).
- Settings › Editing lives in the global Settings page and edits the **current
  project** (the page has no project route).

## Provisional — needs owner confirmation

1. **Projects without a board:** footprint existence/package checks are skipped
   (`footprints_unavailable`, footprint marked `unverified`) rather than loading all
   footprint libraries (3.8 GB). Default as written; alternative: load all
   footprints for such projects.
2. **A compensated symbol delete gives the symbol a new UUID** (re-placed from
   the library, see author calls) — matters once the PCB links to symbol UUIDs
   (sub-project 3). Default as written; alternative: compensate by restoring the
   turn snapshot / last step checkpoint (keeps UUIDs, loses the turn's later work).

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
