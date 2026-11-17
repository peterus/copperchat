# CopperChat Footprint Tools Implementation Plan (index)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> This file is the **index**. Each phase lives in its own file under
> `docs/superpowers/plans/footprints/` and is executed by one child session as
> **exactly one PR**. A phase executor reads, in this order: this index, the
> spec, `docs/spikes/3a-footprints.md`, `docker/kicad-base/patches/README.md`,
> then its own phase file. The editing index (`2026-10-18-editing-plan.md`,
> E-C1…E-C13) and the foundation index stay binding for everything this plan does
> not change.

**Goal:** Let the agent read, check, render, create, approve and delete footprints
in one repo-local library, only through KiCad's IPC API (patched KiCad, PR #85),
with every write verified by read-back, compensated on failure, journalled and
undoable from the History panel.

**Architecture:** A footprint layer next to the schematic edit engine: a raw-protobuf
`FootprintDocument` on the KiCad session (one open footprint doc per server, opened
only after a one-time "activation" that loads the footprint libraries), a pure
`FootprintGeometry` model with a canonical hash, pure `fp_check` rules, a
`kicad-cli` + XML wrapper + CairoSVG render pipeline, and a staged
`FootprintChangeSet` engine (S1 row → S2 seed copy → S3 rebuild) that reuses the
two-phase adapter. History is extended to track the shared library above the
project directory (manifest v2, content-addressed blobs, a write journal for
project-scoped restore).

**Tech Stack:** Python 3.13, FastAPI, SQLite migrations, kipy fork
`peterus/kicad-python` tag `copperchat-e0c9c8e` (raw protobuf for footprint docs),
KiCad `52b43fb8` + patches 0001–0004, `kicad-cli fp export svg`, `xml.etree`,
**new dependency `cairosvg>=2.7.0`**. No frontend component (chat shows PNG tool
images already).

**Spec:** `docs/superpowers/specs/2026-11-17-footprints-design.md` (approved
2026-11-17, owner decisions §14.1 and §15 D1–D4). Spike: `docs/spikes/3a-footprints.md`
(harness `spikes/3a-footprints/`, `fpdoc.py` = reference for the raw protobuf
client). Patch tests: `backend/tests/kicad/test_kicad_api_patches.py` (`FootprintDoc`,
`l201_cycle` = reference implementation for S2/S3 — port it, do not import it).

---

## Phase overview

| # | Phase file | Goal (one line) | Depends on |
|---|---|---|---|
| F1 | `footprints/phase-f1-read-path.md` | `COPPERCHAT_FOOTPRINT_TOOLS`, local-library derivation, activation + `footprints_active`, `FootprintDocument`, `FootprintGeometry` + hash, `fp_get`, `fp_list_local`, `footprint_libraries_ready()`, `invalidate_footprints`, `FP_READ` (part 1) | — |
| F2 | `footprints/phase-f2-check-render.md` | `checks.py` with every §8 rule, `fp_check`; generic `run_kicad_cli`; `fp_render` (kicad-cli → XML wrapper → CairoSVG); renders route `.svg` with CSP | F1 |
| F3 | `footprints/phase-f3-history.md` | migration `0012_footprints.sql`, write-journal repo, `tracked_files`, manifest v2, blob store + GC, scoped library restore, library lock, library-only external change, `library_changed` result field + hash re-recording, generation-check skip | — (parallel to F1/F2) |
| F4a | `footprints/phase-f4a-create-engine.md` | `FootprintChangeSet` + engine S1–S3 with compensation, journal writes + pending cleanup, `fp_create` (incl. `replace`), `leftovers` in `DocumentInconsistent`, wiring of F1 ↔ F3 (locator, listeners), budget weight 5, change-log kinds | F1, F2, F3 |
| F4b | `footprints/phase-f4b-approve-delete-assign.md` | `fp_approve` (+ `footprint_reviews`, `origin="user"` stamping), `fp_delete`, `sch_set_fields`/`sch_place_symbol` local check + `library_row` adapter step, `FP_WRITES` profile | F4a |
| F5 | `footprints/phase-f5-skills-acceptance.md` | `kicad-footprints` skill, skill/prompt updates, research "Land pattern", L201 golden end-to-end, cross-project + undo scenarios, README/CHANGELOG, as-built | F4b |

The spec's P4 is split into F4a/F4b (author call): P4 is the risky part, and one
PR with the engine, three tools, the adapter step and the approval guards would be
too big to review in three rounds. F4a ends with a usable, undoable `fp_create`;
F4b adds the tools that depend on it.

### Execution waves (a phase starts once every phase it depends on is merged)

| Wave | Phases | Note |
|---|---|---|
| 1 | F1, F3 | disjoint files (table below); F3 ships its F1 touchpoints as injectable defaults |
| 2 | F2 | F3 may still be running |
| 3 | F4a | needs F1, F2, **and F3 merged** (spec §17: no write without undo coverage) |
| 4 | F4b | |
| 5 | F5 | |

### File ownership while phases run in parallel (waves 1–2)

| Area | F1 | F2 | F3 |
|---|---|---|---|
| `config.py` (`footprint_tools`) | **owns** | – | reads only via an injected callback (none) |
| `kicad_session/session.py`, new `kicad_session/footprint_doc.py` | **owns** | – | – |
| `library/service.py`, new `library/local.py` | **owns** | reads | – |
| new `kicad_tools/footprints/{model,read,specs}.py` | **owns** | adds `checks.py`, `render` + tool entries in `specs.py` (after F1) | – |
| `kicad_tools/edit/specs_place.py` | `footprints_available` → `footprint_libraries_ready` + wording only | – | – |
| `agent/profiles.py` | `FP_READ = {fp_get, fp_list_local}` | adds `fp_check`, `fp_render` | – |
| `tools/registry.py` | adds the provider line | – | – |
| `kicad_tools/checks.py`, new `kicad_tools/cli.py`, new `views/footprint_render.py`, `web/routes_views.py`, `backend/pyproject.toml` + `uv.lock` | – | **owns** | – |
| `history/*`, new `history/blobs.py`, new `history/library_lock.py`, `db/migrations/0012_footprints.sql`, new `db/repos/footprints.py`, `tools/spec.py`, `agent/sdk_tools.py`, `agent/engine.py` (context wiring) | – | – | **owns** |

F3 does not import anything F1 creates. Where it needs F1 behaviour (library location,
cache invalidation, `ReloadLibrary`), it exposes an injection point with a no-op
default; F4a wires the real implementation (index contract FP-C6). If F1 and F3 both
touch a shared registration file after all, rebase on `main` and keep both sides.

---

## Global Constraints

Every task implicitly includes these (plus the editing and foundation Global Constraints).

- **Write path = KiCad IPC API only** (patches 0001–0004). CopperChat never writes
  `.kicad_mod`, `fp-lib-table` or `.pretty` content, never generates S-expressions,
  never calls `ParseAndCreateItemsFromString`. `kicad-cli` only to render. The only
  file operations on KiCad data: history snapshot/restore of KiCad-written bytes, and
  `rmdir` of an empty library directory the same call (or, on restore, this project)
  created.
- **Fixtures are KiCad-generated** (stock seeds, API-created footprints, copies of
  KiCad-saved files). Never hand-edited, also not in tests. Golden JSON files are
  CopperChat output (`fp_get`), not KiCad files.
- **Nothing on KiCad's GitLab, mailing list or Discord.** No issues, MRs or comments
  by an agent (KiCad AI policy; the owner handles upstream).
- **Never request GitHub Copilot reviews.** Review = `kontor-recipes:review-loop`.
- Pinned KiCad `52b43fb8fc8e11fdab5d683eaefc91a49f9cbcd7` + `docker/kicad-base/patches/0001–0004`;
  kipy fork `copperchat-e0c9c8e`. No fifth KiCad patch in v1 (owner D4).
- Units **mm**, footprint coordinates: origin = anchor, x right, **y down**; angles in degrees.
- New footprint names: `^[A-Za-z0-9][A-Za-z0-9_.+-]{0,99}$`; `lib_id` ≤ 256 chars.
- Library: `<repo_root>/footprints/<nick>.pretty`, nickname = repo dir name reduced to
  `[A-Za-z0-9_-]` (others → `_`, ≤ 32 chars), `_local` appended on a global-table
  collision; project row `scope=LTS_PROJECT`, URI `${KIPRJMOD}/<posix path>`, plugin
  `"KiCad"`, description `"CopperChat local footprints"`.
- Only the local library is writable; every other library is read-only for CopperChat.
- Seed `Inductor_SMD:L_Bourns-SRN4018`; one footprint document per api-server; always
  close before the next open; never close the schematic for footprint work.
- Lock order: `edit_lock` → library lock → `_apply_lock` → `_fp_lock` → `_commit_lock`
  → `_lock`; for restore and step checkpoints `HistoryService.lock` comes first
  (`svc.lock` → library lock → …). None is re-entrant; the library lock is taken once at
  the outermost level. `on_library_changed` runs only after the library lock and
  `_apply_lock` are released and never takes `HistoryService.lock`.
- Budget: `fp_create` (new or `replace`) **5**, `fp_approve` 1, `fp_delete` 1, library
  row 0, read tools 0; limit 20/turn as today.
- `COPPERCHAT_FOOTPRINT_TOOLS` = `on` (default) | `off`; `off` removes `FP_READ` and
  `FP_WRITES` from every profile **including Free**, and makes both activation and
  `footprint_document()` raise `footprint_tools_disabled`.
- Render: `cairosvg>=2.7.0`, refusing `url_fetcher`, never `unsafe=True`; KiCad SVG
  containing `<!DOCTYPE` or `<!ENTITY` → `render_failed`; one `kicad-cli fp export`
  at a time, 60 s timeout, argv list with `--fp=<name>` as one element; ≥ 40 px/mm,
  longest side ≤ 2000 px.
- SVG route headers: `Content-Type: image/svg+xml`, `Content-Security-Policy:
  default-src 'none'; style-src 'unsafe-inline'; sandbox`, `X-Content-Type-Options:
  nosniff`, `Content-Disposition: inline; filename="<lib>_<name>.svg"` (sanitised).
- `fp_check` thresholds exactly as spec §8 (table copied into F2).
- Every write is read back (§9.2: geometry equal within 1 µm) before success; a tool
  error leaves nothing persistent except the named `leftovers` of a failed compensation.

## Review Focus

Failure modes most likely to bite a user although no single happy-path test hits
them. Each names the task that pins it.

1. **Silent overwrite of an existing footprint** — the name exists on disk (another
   project created it, this project has no row yet) or was added in the KiCad GUI
   between `prepare` and S2. Expected: `already_exists`, nothing written, S1 rolled
   back. Tests: F4a Task 2 (`test_name_taken_on_disk_without_row`) and Task 3
   (`test_api_recheck_before_s2_refuses_and_rolls_back_row`).
2. **A restore that undoes another project's footprint work** (shared library) — main
   restores to before its `fp_create` while io has since approved or replaced the same
   footprint. Expected: skipped and listed "changed outside this project", B untouched.
   Tests: F3 Task 5 (unit), F5 Task 3 (KiCad, cross-project).
3. **A half-built seed copy that gets assigned silently** after a server death between
   S2 and S3. Expected: read tools write nothing but flag it unreviewed; the next
   writing call deletes it; a retry with the same name succeeds. Tests: F1 Task 6
   (`unreviewed` classification), F4a Task 4.
4. **CopperChat's own writes reported as external changes** (plan paused, session
   reloaded, 1.7 GB reload) — after a successful, a compensated and a library-row-only
   call. Expected: next turn start reports nothing. Tests: F3 Task 7, F4a Task 5,
   F4b Task 3.
5. **A forged or stale approval** — the agent approves in the same turn, after a
   server-generated user message, with a quote the user never wrote, or after a
   `replace` changed the geometry. Expected: `approval_not_allowed`. Tests: F4b Task 1.

---

## Interface Contracts

Names and types every phase relies on. Signatures are binding; a phase that has to
deviate updates this section in its own PR and adds an "as built" bullet.

### FP-C1. Local library — `copperchat.library.local` (F1)

```python
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,99}$")
LIBRARY_DESCRIPTION = "CopperChat local footprints"

@dataclass(frozen=True)
class LocalLibrary:
    nickname: str
    dir: Path            # <repo_root>/footprints/<nickname>.pretty (may not exist yet)
    uri: str             # "${KIPRJMOD}/../../footprints/<nickname>.pretty" (POSIX)
    repo_root: Path
    table: Path          # <project_dir>/fp-lib-table (may not exist)

def base_nickname(repo_root: Path) -> str                  # sanitise + cut to 32
def derive_local_library(project_dir: Path, repo_root: Path,
                         global_nicknames: Collection[str]) -> LocalLibrary
```

`LibraryService` additions (F1):

```python
async def global_footprint_nicknames(self) -> frozenset[str]  # cached per generation
async def local_library(self, *, for_write: bool = False) -> LocalLibraryInfo | None
#   LocalLibraryInfo(lib: LocalLibrary, row_present: bool, row_conflict: str | None)
#   None while repo_policy is transient (reads); for_write=True raises repo_root_unknown
def local_names_on_disk(self, lib: LocalLibrary) -> list[str]   # *.kicad_mod stems, no parsing
def footprint_libraries_ready(self) -> bool                     # board open or footprints_active
def invalidate_footprints(self, nickname: str | None = None) -> None
async def footprint_geometry(self, lib_id: str, notes: list[dict[str, Any]] | None = None
                             ) -> FootprintGeometry | None  # LRU 64; appends footprint_libraries_loaded
#   never called while the caller holds _fp_lock (activation takes it); read-backs run after
#   the footprint document is closed
def local_footprint_file_safe(lib: LocalLibrary, name: str) -> None  # lstat <dir>/<name>.kicad_mod:
#   if it exists it must be a regular file (no symlink), else library_conflict
def local_library_dir_safe(lib: LocalLibrary) -> None  # lstat every existing component from
#   repo_root to lib.dir: a symlink or non-directory → library_conflict (used by for_write=True)
```

### FP-C2. KiCad session — `copperchat.kicad_session` (F1)

```python
KicadSession.footprints_active: bool          # reset when generation changes
async def KicadSession.activate_footprints(self) -> float | None
#   seconds spent if it activated now, None if already active; raises
#   StructuredToolError(code="footprint_tools_disabled") when settings.footprint_tools
#   is False; StructuredToolError(code="seed_missing") if the seed lib_id is unknown
def KicadSession.footprint_document(self, lib_id: str) -> AsyncContextManager[FootprintDocument]
#   raises footprint_tools_disabled when settings.footprint_tools is False (defence in depth);
#   takes _fp_lock; opens DOCTYPE_FOOTPRINT; close in finally (shielded); a failed close
#   sets KicadSession.restart_pending = True (new flag, F1); F4a makes the turn end
#   (history/hooks.py, F3-owned in wave 1) restart the server when it is set

class FootprintDocument:   # kicad_session/footprint_doc.py, raw protobuf
    async def items(self) -> list[Any]
    async def footprint(self) -> Any                  # FootprintInstance definition
    async def fields(self) -> list[Any]
    async def begin(self) -> None; async def push(self, label: str) -> None; async def drop(self) -> None
    async def create(self, items: list[Any]) -> list[Any]
    async def update(self, items: list[Any]) -> list[Any]
    async def delete(self, ids: list[str]) -> None
    async def update_footprint(self, props: Any) -> None   # patch 0002
    async def save(self) -> None
    async def save_copy(self, target_lib_id: str) -> None  # SaveCopyOfDocument
SEED_LIB_ID = "Inductor_SMD:L_Bourns-SRN4018"
```

### FP-C3. Geometry — `copperchat.kicad_tools.footprints.model` (F1)

```python
@dataclass(frozen=True) class Pad:   number, type, shape, at, rotation, size,
    roundrect_ratio, drill: Drill | None, layers: tuple[str, ...],
    extra: tuple[tuple[str, Any], ...]   # clearance, margins, zone/thermal: hashed only
@dataclass(frozen=True) class Shape: kind, layer, points, center, size, width, closed
@dataclass(frozen=True) class Text:  kind, layer, at, size, value
@dataclass(frozen=True) class Model3D: path, offset, rotation, scale
@dataclass(frozen=True)
class FootprintGeometry:
    lib_id: str; scope: Literal["local", "project", "global"]
    description: str; keywords: str; mounting: Literal["smd", "tht", "unspecified"]
    exclude_from_bom: bool; exclude_from_pos: bool; dnp: bool
    fields: dict[str, str]; pads: tuple[Pad, ...]; shapes: tuple[Shape, ...]
    texts: tuple[Text, ...]; models: tuple[Model3D, ...]
    overrides: tuple[tuple[str, Any], ...]          # patch-0002 properties, hashed
    def bbox(self) -> tuple[float, float, float, float]       # pads + shapes
    def layer_summary(self) -> dict[str, dict[str, Any]]
    def to_dict(self, detail: Literal["summary", "full"] = "summary") -> dict[str, Any]

def geometry_from_proto(definition: Any, items: Sequence[Any], lib_id: str, scope: str) -> FootprintGeometry
def geometry_hash(g: FootprintGeometry) -> str      # sha256 hex, §9.4, 1 nm rounding
def geometry_equal(a: FootprintGeometry, b: FootprintGeometry, tol_mm: float = 1e-3) -> list[str]  # diffs
REVIEW_FIELD = "needs_user_review"; SOURCE_FIELD = "Footprint source"; APPROVAL_FIELD = "User review"
RESERVED_FIELDS = {"reference", "value", "footprint source", "needs_user_review", "user review"}
#   (casefolded) never accepted as fp_create extra fields; only fp_approve writes
#   needs_user_review = "no" and User review
def review_state(g: FootprintGeometry, *, pending: bool) -> Literal["approved", "needs_user_review", "unreviewed"]
```

`review_state` (resolves spec nit "without source or review flag"): **unreviewed** when the
footprint is local and **either** `Footprint source` **or** `needs_user_review` is
missing, or a `pending` journal row exists; **needs_user_review** when the field is
`yes`; **approved** when it is `no` (with `User review`).

### FP-C4. Checks — `copperchat.kicad_tools.footprints.checks` (F2)

```python
@dataclass(frozen=True) class Finding: rule, severity, message, pads=(), at=None, measured=None, expected=None
@dataclass(frozen=True) class Expected: pads, pitch, courtyard, body, tolerance_mm=0.01
@dataclass(frozen=True)
class CheckInput:
    geometry: FootprintGeometry
    pins: list[str] | None = None          # symbol pin numbers
    polarized: bool = False
    expected: Expected | None = None
    allow_unmatched: frozenset[str] = frozenset()
    local: bool = False
    pending: bool = False
def run_checks(inp: CheckInput) -> CheckReport   # pure; CheckReport(passed, counts, findings, geometry_hash)
def to_result(report: CheckReport) -> dict[str, Any]
#   counts stay exact; findings capped at 20 per rule and 200 overall, with
#   truncated: true and omitted: {rule: n}
```

### FP-C5. CLI + render (F2)

```python
# kicad_tools/cli.py
@dataclass(frozen=True) class CliResult: returncode: int; stdout: str; stderr: str
async def run_kicad_cli(settings: Settings, args: Sequence[str], *, timeout: float,
                        cwd: Path | None = None) -> CliResult   # isolated env/config, kill+reap
# views/footprint_render.py
async def render_footprint(ctx: ToolContext, lib_id: str, layers: list[str] | None,
                           grid_mm: float | None, labels: bool) -> RenderOutput
#   RenderOutput(png: Path, svg: Path, scale_px_per_mm: float, extent_mm: tuple[float, float], geometry_hash: str)
```

### FP-C6. History — `copperchat.history` (F3)

```python
@dataclass(frozen=True)
class LibraryLocation:            # what history needs; F4a adapts LocalLibrary to it
    dir: Path; repo_root: Path; table: Path; nickname: str
@dataclass(frozen=True)
class TrackedFile: path: Path; rel: str; library: bool   # rel = project-relative POSIX, may start "../"
def tracked_files(project: Project, library: LibraryLocation | None) -> list[TrackedFile]
LibraryLocator = Callable[[Project], Awaitable[LibraryLocation | None]]
HistoryService.locate_library: LibraryLocator          # default: async lambda _: None
#   per Project, also for projects that are not open (restore of a closed project, C14):
#   F4a builds it from repo_policy + derive_local_library + global nicknames (current
#   session's LibraryService if any, else the stock-dir fallback of author call 9)
HistoryService.library_listeners: list[Callable[[Project, list[str], str], Awaitable[None]]]
#   (project, changed rel paths, reason in {"own", "external", "restore"})
async def HistoryService.on_library_changed(self, project: Project, files: Iterable[Path]) -> None
#   re-records only these files' hashes (+ stats), calls listeners with reason "own"
def library_lock(library_dir: Path) -> asyncio.Lock      # history/library_lock.py, process-wide
def assert_library_locked(library_dir: Path) -> None
# tools/spec.py
ToolResult.library_changed: set[str]                         # nicknames (success only)
ToolContext.library_touched: list[Path]                      # appended by every write stage,
#   incl. compensation and cleanup; the context is built per tool call
ToolContext.on_library_changed: Callable[[list[Path]], Awaitable[None]] | None
#   the adapter calls it in `finally` (shielded) whenever library_touched is non-empty,
#   for successful and failed calls alike — after the library lock and _apply_lock are
#   released; it never takes HistoryService.lock
```

Manifest v2 (written by every snapshot from F3 on):

```jsonc
{"version": 2, "id": "…", "projectId": "…", "runId": "…", "createdAt": "…", "label": "…",
 "kind": "turn", "planStepId": null,
 "repoRootRel": "hardware/main",                    // project dir relative to the repo root
 "journalMaxId": 41,                                // highest footprint_writes.id at snapshot time
 "files": [{"relPath": "main.kicad_sch", "sha256": "…", "size": 1}],   // project files, copied as today
 "library": {"dir": "footprints/X.pretty",          // repo-root-relative; null while transient
             "existed": true,
             "files": [{"relPath": "../../footprints/X.pretty/L.kicad_mod", "sha256": "…", "size": 1}]},
 "table": {"existed": false}}                       // the project's fp-lib-table; never copied back
```

Library files live as blobs in `.kicad-agent/history-blobs/<sha256>`; the project's
`fp-lib-table` is not copied into snapshots (restore only removes this project's own rows,
spec §10).

### FP-C7. Database — `0012_footprints.sql` (F3, used by F4a/F4b)

```sql
ALTER TABLE document_hashes ADD COLUMN size INTEGER;      -- NULL = never reused
ALTER TABLE document_hashes ADD COLUMN mtime_ns INTEGER;
ALTER TABLE document_hashes ADD COLUMN inode INTEGER;
CREATE TABLE history_blob_refs (
  project_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, sha256 TEXT NOT NULL,
  PRIMARY KEY (snapshot_id, sha256));
CREATE INDEX history_blob_refs_project ON history_blob_refs(project_id, sha256);
CREATE TABLE footprint_writes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL, run_id TEXT, lib_id TEXT NOT NULL,
  file_rel TEXT NOT NULL,           -- repo-root-relative POSIX path of the .kicad_mod / fp-lib-table
  op TEXT NOT NULL CHECK (op IN ('row_add','create','replace','approve','delete')),
  state TEXT NOT NULL CHECK (state IN ('pending','done','compensated','leftover')),
  pre_sha256 TEXT, post_s2_sha256 TEXT, post_sha256 TEXT,   -- sha hex or 'absent'
  geometry_hash TEXT, created_at TEXT NOT NULL, finalised_at TEXT);
CREATE INDEX footprint_writes_project ON footprint_writes(project_id, state, created_at);
CREATE INDEX footprint_writes_file ON footprint_writes(file_rel, id);
CREATE TABLE footprint_reviews (
  conversation_id TEXT NOT NULL, lib_id TEXT NOT NULL, geometry_hash TEXT NOT NULL,
  run_id TEXT, rendered_at TEXT, checked_at TEXT, check_errors INTEGER,
  PRIMARY KEY (conversation_id, lib_id, geometry_hash));
```

`file_rel` is **repo-root-relative**, so two projects of one repo name the same file
the same way (the cross-project guard compares it). `db/repos/footprints.py` (F3):
`FootprintWriteRepo.insert_pending(...) -> int`, `.set_post_s2(id, sha)`,
`.finalise(id, state, post_sha256)`, `.pending(project_id)`,
`.latest_for_file(file_rel)`, `.later_foreign(file_rel, project_id, after_id) -> bool`,
`.max_id() -> int`, `.after_id(project_id, after_id) -> list[Row]`;
`FootprintReviewRepo` is added by F4b in the same module.

### FP-C8. Write path — `copperchat.kicad_tools.footprints.engine` (F4a)

```python
NO_SHEET = SheetRef(path="", page="", file="", name="<footprint>")
@dataclass(frozen=True) class LibraryRowStage: library: LocalLibrary; create_library: bool
#   nickname/uri/dir come from `library`
@dataclass(frozen=True) class FpStage: kind: Literal["ensure_library","copy_seed","rebuild","approve","delete"]; ...
@dataclass
class FootprintChangeSet(ChangeSet):      # sheet=NO_SHEET, stages=[], pre_images={}, intent=NetIntent()
    fp_stages: list[FpStage] = field(default_factory=list)
    target: FootprintGeometry | None = None
    pre_image: FootprintPreImage | None = None     # replace only (API data)
    library: LocalLibrary | None = None
    fail_after: str | None = None                  # test hook: raise InjectedFailure after a stage
    kill_after: str | None = None                  # test hook: kill the api-server after a stage
    fail_compensation: bool = False                # test hook: DeleteLibraryItem raises
ChangeSet.library_row: LibraryRowStage | None = None      # new base field (default None)
async def apply_footprint_change_set(ctx: ToolContext, cs: FootprintChangeSet) -> FootprintApplied
async def run_library_row(ctx: ToolContext, stage: LibraryRowStage, *, verify: str | None) -> bool
#   lock-free; asserts library_lock(stage.library.dir); re-runs local_library_dir_safe first
# apply_footprint_change_set also re-runs local_library_dir_safe (and, for replace/approve/
# delete, local_footprint_file_safe) under the library lock before its first write
async def cleanup_pending(ctx: ToolContext, lib: LocalLibrary) -> list[dict[str, Any]]   # notes
def classify_pending(rows, current_sha: Callable[[str], str]) -> list[PendingOutcome]   # pure, used by prepare too
```

`apply_change_set` (schematic engine) raises `TypeError` on a `FootprintChangeSet`.
`DocumentInconsistent` gains `leftovers: list[str]` (default `[]`) and `footprint_only: bool`;
the run engine carries both to `TurnOutcome` for `_recovery_notice`, and a turn whose
inconsistencies are all `footprint_only` skips the schematic reopen. `KicadUnavailable` (or a generation
change) during a stage **skips compensation**: journal rows stay `pending` (spec §9.3
server-death case) and the error is re-raised.

### FP-C9. Error codes (all phases)

`not_found`, `invalid_argument`, `library_error`, `footprint_tools_disabled`,
`seed_missing`, `repo_root_unknown`, `library_conflict`, `not_local`,
`already_exists`, `check_failed`, `render_failed`, `verification_failed`,
`document_inconsistent`, `approval_not_allowed`, `in_use`. Notes (`notes[].kind`):
`footprint_libraries_loaded` (`seconds`), `footprint_needs_review`,
`footprint_review_unknown`, `footprint_unverified`, `possibly_in_use_elsewhere`,
`model_missing`, `pending_cleaned`, `pending_leftover`.

---

## As built

(Filled by each phase PR: one bullet per deviation from FP-C1…FP-C9 or from a
phase file, with the reason.)

## Residuals

(Filled by F5.) Known before the start:

- Flaky `test_streamed_text_is_persisted_by_size_and_by_time` (timing) and kicad-shard
  submodule fetch errors: rerun the failed job (`gh run rerun --failed`), do not
  "fix" in a footprint PR.
- Footprint renders (`.png`, `.svg`, `.svg.json`) in `.kicad-agent/renders/` are not pruned,
  like `render_view` output today.
- Board sessions get `FP_WRITES` with SP4; the "Approve" button with the SP4 frontend
  work (D1); `fp_import` for vendor libraries later (D2); lazy-load KiCad patch only if
  PROD memory gets tight (D4); IPC-7351 generation and real 3D models later.

---

## Decided by owner

Binding (spec §14.1 and §15, 2026-11-15 … 2026-11-17):

1. API only with KiCad patches (B); no hand-written KiCad files.
2. One library per repo at the repo root, `${KIPRJMOD}`-relative project row.
3. Approval flow A in chat; `needs_user_review` until the user says ok; v1 uses the
   guarded `fp_approve` tool (D1), a button follows with SP4.
4. v1 tools `fp_get`, `fp_create`, `fp_check`, `fp_render`, `fp_delete`,
   `fp_list_local`, plus the `sch_set_fields` existence check for local libraries.
5. Manufacturer/third-party libraries: user adds them; read-only for CopperChat (D2).
6. `fp_create` costs 5 logical changes (D3).
7. No fifth KiCad patch in v1; `COPPERCHAT_FOOTPRINT_TOOLS` switch + documented RAM need (D4).
8. Undo/history cover the library and `fp-lib-table`; memory cost only for sessions that
   use footprint tools; `fp_get`/`fp_render` in all session types, writes in Schematic + Free.

## Calls made by the plan author

These resolve the spec nits deferred at the spec merge (memory
`feature/footprints/progress`) and a few gaps found while planning. Each is pinned by a
test in the named phase.

1. **Blob reuse key storage** (spec §10 "Hashing"): `(size, mtime_ns, inode)` is stored
   in three new **nullable** columns of `document_hashes` (FP-C7). Existing rows get
   `NULL` and are simply hashed on first use, so no data migration. The stat is taken
   **before** hashing and again after; if the two stats differ, the hash is stored
   without stat (never reused). F3 Task 3.
2. **Blob GC** (§10 "Pruning"): references are kept in `history_blob_refs`, written in
   the same transaction as the `history_snapshots` row and deleted with it. GC runs only
   when `_prune` removed at least one snapshot (or found temp files) and compares the
   blob directory listing with `SELECT DISTINCT sha256 … WHERE project_id = ?` — no
   manifest is parsed. Blob writes and GC run under a per-project blob lock, so every
   `.tmp-*` file seen by GC is a crash leftover and is removed. F3 Task 4.
3. **SVG download name** (§11): `fp_render` writes a sidecar `<uuid>.svg.json`
   `{"download_name": "<lib>_<name>.svg"}` next to the SVG. The route never serves
   `.json` (pattern is `^[0-9a-f]{32}\.(png|svg)$`), reads the sidecar if present,
   reduces the name to `[A-Za-z0-9_.+-]` (≤ 120 chars, must end in `.svg`), and falls
   back to `<uuid>.svg`. F2 Task 6.
4. **Restore and the library lock** (§10): narrowed. Restore holds the library lock
   only around the library **file** part (plan, copy-back, deletes, `rmdir`), not
   across session close/reopen. The `DeleteLibraryTableEntry` row removal after the
   reopen runs under the project's history lock only: the `fp-lib-table` belongs to
   this project, and the agent is idle (restore refuses `agent_busy`). So another
   project's snapshot waits at most for the file copy, never for a KiCad restart.
   F3 Task 6.
5. **`COPPERCHAT_FOOTPRINT_TOOLS=off` and `pending` rows** (§9.3): accepted as is. With
   `off` no write can happen, so no cleanup runs; the rows stay `pending`, and
   `review_state` keeps the footprint `unreviewed` (assignment carries
   `footprint_needs_review`). History undo still covers them. Turning the switch back on
   lets the next writing call clean up. The README says so. F1 Task 6, F5 Task 4.
6. **"missing either"** (§9.3 point 4): unreviewed = **either** `Footprint source` **or**
   `needs_user_review` missing, or a `pending` journal row (FP-C3 `review_state`).
   `source_missing` stays an **error for every local footprint**, as spec §8 says
   (a crashed seed copy looks hand-made from a sibling project, so no weaker rule is
   safe). A footprint the user put into the local directory by hand therefore fails
   `fp_check` until it is re-created with `fp_create(replace=true)` and a source; the
   `kicad-footprints` skill tells the agent to explain this. F2 Task 2.
7. **Journal test gaps**: added — pending `replace` → `leftover`; every read path
   (`fp_get`, `fp_list_local`, `fp_check`, a prepare-only `sch_set_fields` through
   `footprint_notes`, activation) leaves journal and files unchanged (F4a Task 4); a `leftover` later changed by another project is skipped by
   restore; a generation change between the crash and the cleanup does not matter (the
   cleanup compares file hashes only). F3 Task 5, F4a Task 4.
8. **`FootprintChangeSet.sheet`**: the base `ChangeSet.sheet` stays `SheetRef` (mypy);
   footprint sets carry the sentinel `NO_SHEET` instead of `None`. Gates only read
   `cs.tool` and `cs.logical`. F4a Task 1.
9. **Global nicknames before activation** (§5): F1 Task 1 probes whether
   `GetLibraryTable(LT_FOOTPRINT, LTS_GLOBAL)` answers on a boardless server before the
   PCB kiface is loaded. If yes, it is used directly (cached per generation). If no,
   the global nickname set is the list of `*.pretty` directory **names** in the stock
   footprint directory (`KICAD<n>_FOOTPRINT_DIR` of the api-server env; no file is
   read), which equals the image's global table; F1 records the probe result here under
   "As built".
10. **Phase split**: spec P4 → F4a + F4b (see phase overview).
11. **Journal `file_rel` is repo-root-relative**, and the migration adds
    `post_s2_sha256` and `finalised_at`, which spec §9.3 uses but did not list.
12. **Migration ownership**: F3 creates all footprint tables in one migration
    `0012_footprints.sql` (spec: "same migration"), because restore (F3) already reads
    the journal.
13. **`fp_approve` and plans**: `fp_approve` is exempt from the plan refusals in
    **both** gates — `BudgetGate.before_prepare` ("no active plan step" under policy
    `always`) and `PlanGate.before_prepare` (`plan_open` for proposed/approved/paused
    plans, `plan_step_required`) — because it records a user decision, not a design edit;
    guard 6 still refuses it while a plan is `running`. F4b Task 1.
14. **Restore of a project that is not open**: no API is available, so the table is left
    untouched and every own `row_add` row since the snapshot is listed in the notice as
    "library row <nick> not removed (project not open)"; `remove_table` is never taken
    in that case. F3 Task 6.
15. **`user_quote`** must hold at least 2 words and 8 non-space characters and match on
    word boundaries (or be the whole message when that is shorter); a stricter reading of
    spec guard 5, not a weaker one. F4b Task 1.
16. **Output caps**: `fp_get` returns at most 256 pads (`summary`) / 1,000 pads (`full`)
    with `pads_truncated: true` and an exact `pad_count`; `full` returns at most 500 shapes
    and 64 points per shape (`truncated: true`, note pointing to `fp_render`); `fp_check`
    findings are capped (FP-C4), also in `fp_create`'s `check_failed` details. F1 Task 5,
    F2 Task 2, F4a Task 2.
17. **Journal boundary**: the manifest stores `journalMaxId` (the highest `footprint_writes.id`
    at snapshot time); restore considers rows with `id > journalMaxId`, never timestamps.
    F3 Tasks 1, 4, 5.
18. **Numeric and string bounds** for every footprint tool input: coordinates |x|,|y| ≤ 500 mm,
    sizes 0 < s ≤ 200 mm, drills 0 < d ≤ 50 mm, finite rotations, pad numbers ≤ 16 chars,
    `symbol` ≤ 256, layers from the fixed layer-name pattern; extra field names
    `^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$` (checked after NFKC + whitespace collapse, before
    the `RESERVED_FIELDS` casefold check). F2 Task 3, F4a Task 2.

## Provisional — needs owner confirmation

None.

---

## Phase Conventions (apply to every phase)

- **Branch/PR:** one PR per phase, title `footprints phase <F#>: <short goal>`, via
  `gh` CLI (GitHub MCP is broken). Commits end with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, PR bodies with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- **TDD:** failing test → see it fail → minimal code → pass → commit, per task.
- **Commands** (from `backend/`): `uv run pytest -m "not kicad"`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy src`; KiCad tests `uv run pytest -m kicad
  tests/kicad/footprints` inside `ghcr.io/peterus/copperchat-kicad-base:<KICAD_COMMIT>`
  (the patched base image of `52b43fb8`; see `tests/kicad/test_kicad_api_patches.py`
  for the invocation). Long commands always with `timeout`.
- **Real-KiCad tests** live in `backend/tests/kicad/footprints/` and use
  `blank_session`, `blank_nopcb_session` and (from F1) `repo_session`. Activation costs
  ~3 s and ~1.7 GB per api-server: share one module-scoped server where isolation
  allows; the CI kicad job is sharded 8× (`tests/support/sharding.py`).
- **Unit tests** in `backend/tests/unit/footprints/` (new) and the existing
  `tests/unit/history/`, `tests/unit/library/`. Never mock KiCad in a `-m kicad` test.
- **Review:** after CI is green, `kontor-recipes:review-loop --max-rounds 3
  --dimensions bugs+tests,security+performance`, a fresh reviewer child per round;
  pure style nits are deferred after round 2; if the cap is hit with code changes, one
  `--review-only` verify pass before reporting. **Never Copilot.**
- **Done report:** `kontor_send_to_session(<coordinator>, status:'done')` with PR, CI,
  review result and deviations; contract deviations are reflected in this index in the
  same PR (section "As built").
- **Never**: hand-write or hand-edit a KiCad file (also not fixtures), act on KiCad's
  GitLab, request Copilot, close the schematic for footprint work, `pkill -f`/`killall`
  (only own PIDs).
- **No placeholders** in merged code; injectable no-op defaults only where this index
  names the phase that wires them (F3 → F4a).
