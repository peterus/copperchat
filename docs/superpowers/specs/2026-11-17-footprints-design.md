# CopperChat — Footprint tools (design)

Date: 2026-11-17
Status: draft for owner review. The owner decided the main points in chat between 2026-11-15
and 2026-11-17; §14 lists them. Decisions the author made alone are marked
**(author call)** and are listed again in §14.2. The open owner questions are in §15.

Inputs:

- the owner's original feature spec (a CopperChat session on the OpenDriveHub project);
- spike 3A (`docs/spikes/3a-footprints.md`);
- the KiCad API patches in `docker/kicad-base/patches/` (PR #85, released in v0.11.0);
- the existing editing design (`2026-10-17-editing-design.md`);
- the sessions design (`2026-10-19-sessions-design.md`).

## 1. Purpose

### Why

Today CopperChat can only assign footprints that already exist in a library. When it cannot find
an exact stock match, the agent can only name the closest footprint and tell the user. The
`kicad-schematic-editing` skill says so: "creating library symbols or footprints is not something
you can do". Boards (SP4) need a correct footprint for every part. So footprints come before the
board, by owner decision.

The owner's acceptance case comes from the real OpenDriveHub board:

- **L201 = Bourns SRN3012BTA-2R2M.** KiCad has no stock footprint for it. The footprint
  `L_Bourns_SRN3012BTA` must have:
  - two SMD rect pads of 1.3 × 3.5 mm at x = ±1.1 mm, pad 1 on the left;
  - F.CrtYd 4.0 × 4.0 mm and F.Fab 3.0 × 3.0 mm;
  - a silk marker outside the pads;
  - a cited source;
  - the `needs_user_review` flag until the owner approves it.
- Later checks on the same board, which use the check tools only:
  - **J402**, a microSD socket: the symbol has 9 pins but the footprint has 11 pads (2 shell pads).
    `fp_check` must explain the difference, not hide it, and not fail it.
  - **U202 = TI TPS63000 in the DRC0010J package (VSON-10).** Check the stock KiCad footprint
    against the datasheet land pattern before it is used.

### What the agent can do after this

- read any footprint (stock or local) with its full geometry, without a board;
- create a footprint in the repo's own library, from datasheet dimensions with a cited source;
- check a footprint against the symbol's pins and against basic manufacturability rules;
- render a footprint with a mm grid so that the user can approve it in chat;
- list, approve and delete the repo's local footprints;
- assign a local footprint to a symbol, after checking that it exists.

## 2. Scope

### Goals (v1)

1. Read-only tools `fp_get`, `fp_list_local`, `fp_check` and `fp_render`, available in every
   session type.
2. Writing tools `fp_create`, `fp_approve` and `fp_delete` in Schematic and Free sessions. Board
   sessions get them with SP4.
3. One local library per repo at the repo root. Every project of the repo references it through a
   `${KIPRJMOD}`-relative row in its project `fp-lib-table`, written through the API.
4. Approval in chat (owner flow A). Every footprint CopperChat creates carries a source and stays
   marked `needs_user_review` until the user approves it in chat.
5. Undo and history cover the local library and the `fp-lib-table`, although both can lie above
   the project directory.
6. `sch_set_fields` / `sch_place_symbol` check that a footprint in the local library exists, also
   in projects without a board.
7. Skills: when to use which library, and how to create a footprint with a cited source.

### Non-goals (v1)

- IPC-7351 generators or templates (density levels, automatic land-pattern computation). This is
  planned for later.
- Real 3D models. v1 only stores a **link** to an existing STEP/WRL file (path and transform);
  CopperChat never generates a model.
- Editing stock (KiCad) footprints, or footprints in libraries that CopperChat did not create.
- Symbol creation. Footprints only.
- An interactive footprint viewer in the web UI. KiCanvas cannot open `.kicad_mod`; this is a
  later option (§4).
- Board-level checks against the project's design rules (`.kicad_dru`, board setup). v1 uses
  fixed, board-agnostic thresholds (§8). The project's rules come with SP4.
- Downloading manufacturer or third-party footprints (see §15 Q2).

### Anti-goals (the feature fails if one of these happens, even if it "works")

- **Invented dimensions.** The agent creates a footprint whose dimensions are not backed by a
  cited source: a datasheet page or figure, or a recorded user statement. `fp_create` refuses a
  call without `source`. The skill forbids guessing missing numbers; the agent must ask instead.
- **Hand-written KiCad files.** CopperChat must not write any `.kicad_mod`, `fp-lib-table` or
  `.pretty` content itself, and must not generate S-expressions or call
  `ParseAndCreateItemsFromString`. All writes go through the KiCad IPC API. `kicad-cli` is used
  only to render. The only file-system operations CopperChat does on KiCad data are history
  snapshot and restore (as for schematics today) and removing an empty directory that it created
  in the same failed call (§9.3).
- **Agent action on KiCad upstream.** No issues, MRs, comments or mailing-list posts on KiCad's
  GitLab or anywhere else. The upstream packages for the patches are the owner's business.
- **Silent approval.** A footprint counts as approved although the user never saw the render and
  the check result and never said so.
- **Silent overwrite.** `SaveCopyOfDocument` overwrites without asking (spike 3A Q2). CopperChat
  must check the name before every save.
- **A failed tool that leaves things behind.** If a tool reports an error, nothing persistent is
  left: no half-built footprint, no orphan table row (§9.3).
- **A change that cannot be undone** from the History panel.

## 3. Constraints

From the spike, the patches and the existing system:

| # | Constraint | Source |
|---|---|---|
| C1 | There is no "new footprint" API call. A new footprint = open a stock **seed** → `SaveCopyOfDocument("lib:Name")` → close the seed → open the copy → rebuild it in **one** commit → `SaveDocument` → close. | spike Q1 |
| C2 | Only **one** footprint document may be open per api-server. Always close it before opening the next. Patch 0004 makes a second open replace the first, but CopperChat does not rely on that. | spike Q4, patch 0004 |
| C3 | Closing the **last** open document unloads the project. The schematic always stays open, so this is safe as long as CopperChat never closes the schematic for footprint work. | spike Q4 |
| C4 | Every `OpenDocument(DOCTYPE_FOOTPRINT)` runs `AsyncLoad` + `BlockUntilLoaded` on the footprint adapter, so the first one loads **every** footprint library in the tables: about +1.7–1.8 GB RSS and about 3 s. This also applies on servers **with** a board, where reads alone would load libraries lazily. Opening a footprint document, or `AddLibraryTableEntry(create_library)`, also registers the PCB kiface library handler on boardless servers. Up to 4 GB was observed under heavy reads. Once loaded, `GetItemsFromLibrary` is fast: 401 footprints in 0.2 s. | spike Q5, patch notes |
| C5 | Library table rows can be written only through patch 0001: `Add/Update/DeleteLibraryTableEntry` with `scope = LTS_PROJECT`. Pass `${KIPRJMOD}`-relative URIs, never substituted ones. `create_library = true` creates the empty `.pretty` directory, including parent directories, and is rolled back on failure. Status codes: `LCS_OK`, `LCS_ALREADY_EXISTS`, `LCS_NOT_FOUND`, `LCS_READ_ONLY`, `LCS_ERROR`. The tested refusal goes the other way: a **global** row with `create_library` is refused (`LCS_ERROR`, nothing created) when a **project** row with the same nickname shadows it. A project row that reuses a global nickname is not refused; it silently shadows the stock library. | patch 0001, `test_kicad_api_patches.py` |
| C6 | Properties of the footprint itself can be written only through patch 0002, `UpdateItems(FootprintInstance)` on the open footprint document. These are: description, keywords, attributes (SMD/THT, exclude from BOM/position files), overrides, net ties, private layers, jumpers, height and 3D models. Custom fields are separate items (`Field`). | patch 0002 |
| C7 | Footprints can be deleted only through patch 0003, `DeleteLibraryItem{LT_FOOTPRINT, lib_id}`. | patch 0003 |
| C8 | kipy's `Board` wrapper cannot handle a footprint document: fields fail, and `save_as` resolves the wrong path. Footprint documents need raw protobuf. The starting point is `FootprintDoc` in `backend/tests/kicad/test_kicad_api_patches.py`. | spike Q6 |
| C9 | `GetDocumentModifiedState` always answers `DMS_UNKNOWN` for footprints. CopperChat cannot ask KiCad whether a footprint is dirty, so it must always save explicitly. | spike Q6 |
| C10 | `kicad-cli fp export svg` writes in mm (the `viewBox` is the size in mm) with y pointing down. It **exits 0** even when it failed to write (a missing output directory). The output file must be checked. | spike Q8 |
| C11 | KiCad 11 footprint files (`version 20261031`) cannot be read by KiCad 10. | spike |
| C12 | History snapshots cover only `.kicad_pro`, `.kicad_sch` and the board **below the project directory** (`history/store.document_files`). Restore copies manifest files back but never deletes files that are missing from the manifest. | `history/store.py`, `restore.py` |
| C13 | The schematic edit engine (`kicad_tools/edit/engine.py`) handles schematics only (sheet commits, netlist verification), so footprint writes need their own apply path. The adapter steps (read-only check, edit lock, gate, `prepare`, budget, snapshot, `apply`) stay the same. | `agent/sdk_tools.py` |
| C14 | Only one KiCad session is "current" per CopperChat server (`KicadSessionManager`). One api-server serves one project at a time. | `kicad_session/session.py` |

## 4. Approaches considered

| Approach | Verdict |
|---|---|
| **A. API only, with patched KiCad.** Patches 0001–0004 in `kicad-base`; footprint content via the footprint document; rows, properties and deletes via the new commands. | **Chosen.** All writes go through KiCad; the file format is always current; read-back is exact (spike Q1: `fp upgrade` leaves the file byte-identical). |
| B. CopperChat writes `.kicad_mod` and table rows itself, as S-expression text. | **Rejected by the owner.** The foundation's MCP tools did exactly this and wrote broken files. It also breaks the rule "everything KiCad through the API". |
| C. Only the table row as a file (stop-gap "A" in spike 3A). | No longer needed: patch 0001 provides the API for it. |
| D. A KiCanvas-based interactive footprint viewer, so the user can approve by inspecting. | **Deferred.** KiCanvas has no `.kicad_mod` loader (only sch/pcb) and no measure tool. A fork patch could wrap the footprint in an in-memory board. v1 uses `fp_render` images in chat. |
| E. Generate land patterns from IPC-7351 (pad size from package dimensions). | **Later.** v1 takes explicit pad geometry from the datasheet's recommended land pattern. |

## 5. Library location, naming and table row

Owner decision B: **one library per repo, at the repo root**, shared by all projects in the repo.

- **Root.** The root is `repo_policy(project).root` (`repo_context/paths.py`): the git toplevel
  when it is the project directory or an ancestor of it, and the project directory otherwise
  (non-git projects).
- **Stable root.** While `repo_policy` reports `transient` (git failed or timed out, so the root
  fell back to the project directory), every footprint **write** is refused with
  `repo_root_unknown`. Snapshots skip the library part and restore leaves it untouched in that
  state (§10). Otherwise a git hiccup would create a second library in the project directory.
- **Directory.** `<root>/footprints/<nick>.pretty`. This one directory is **the local library**.
- **Nickname** (author call). The repo root directory name, cut down to `[A-Za-z0-9_-]` (other
  characters become `_`, at most 32 characters). Example: `OpenDriveHub`.
  - If that name is already a nickname in the **global** table, `_local` is appended, and the
    directory name follows it. A project row would silently shadow the stock library (C5), and
    that would confuse lib ids.
  - The derivation depends only on the repo directory name and the global table. The global table
    is the same for every project of the CopperChat server. So every project of the repo derives
    the **same** nickname and the same directory, and the nickname needs to be stored nowhere.
- **URI.** `${KIPRJMOD}/` followed by the path from the project directory to the library,
  POSIX-style. Examples: `${KIPRJMOD}/../../footprints/OpenDriveHub.pretty` for
  `hardware/main/main.kicad_pro`; `${KIPRJMOD}/footprints/x.pretty` for a project at the root.
- **Row.**
  `AddLibraryTableEntry(type=LT_FOOTPRINT, scope=LTS_PROJECT, nickname, uri, plugin="KiCad",
  description="CopperChat local footprints", create_library=true)`.
  - `LCS_ALREADY_EXISTS` is fine when the existing row has the same URI.
  - A row with the same nickname but a different URI is an error (`library_conflict`). The
    footprint tool then refuses, and the message names both URIs.
  - A row that points to the local directory under **another** nickname (e.g. one the user added
    in the GUI) is also a `library_conflict`. The message asks the user to rename or remove that
    row. CopperChat does not reuse it, because the projects of the repo would then use different
    lib ids for the same footprint.
- **When.** The row is created lazily, by the first `fp_create` in a project, as the first stage
  of that call (§9.3). The other projects of the repo get their row the first time a footprint
  tool writes in them, or when `sch_set_fields` assigns a local footprint there (§7.9).
- **Local library** = exactly `<root>/footprints/<nick>.pretty` under the nickname `<nick>`.
  `fp_delete`, `fp_create(replace)`, `fp_approve`, the `source_missing` rule and the history
  coverage (§10) work only on it. **Every other library is read-only for CopperChat.** That
  includes other `.pretty` directories inside the repo, such as a vendor library committed by
  the user (§15 Q2). `fp_list_local` lists only the local library.

`LibraryService` (`library/service.py`) gets these methods:

- `local_library()` returns `(nickname, uri, path, row_present)`. The name and path are derived
  as above; `row_present` comes from `GetLibraryTable(LT_FOOTPRINT, LTS_PROJECT)`.
- `ensure_local_library()` is used inside `apply` only.
- `local_names_on_disk()` lists `*.kicad_mod` **file names** in the local directory. It is
  read-only and parses no content, and it is used only while the project has no row, when the API
  cannot list the library (§7.6, §7.9).
- `invalidate_footprints(nickname | None)` clears every footprint cache: `_fp_scopes`,
  `_fp_libs[nickname]` and its failure entry, and the `fp_get` LRU. It is called after every
  footprint stage (row added, footprint written or deleted, approval), after a history restore,
  and after an external library change (§10). Otherwise `footprint_notes` and
  `lib_search_footprints` would answer from stale data in the same turn.

## 6. Footprint access in the KiCad session

### 6.1 Activation and memory

- The **footprint handler** gets a new flag, `KicadSession.footprints_active` (author call). It is
  set the first time a footprint tool needs the PCB kiface library handler on a server without a
  board. To activate, CopperChat opens a pinned stock footprint document and closes it again.
  This step costs about 3 s and +1.7 GB (C4). The flag resets when the server restarts
  (`generation` changes).
- `footprint_libraries_ready()` = board open **or** `footprints_active`. It replaces
  `LibraryService.footprints_available()` for every caller: `footprint_notes`,
  `lib_search_footprints` and the new tools.
- **Who triggers it.**
  - Only `fp_*` tools trigger activation, plus `sch_set_fields` / `sch_place_symbol` with a
    footprint from the **local** library.
  - Assigning a **stock** footprint in a boardless project does not activate anything. It keeps
    the `footprint_unverified` note, unless the handler is already active.
  - As a result, the memory cost hits only servers whose sessions actually use footprint tools,
    as the owner decided.
  - **Board projects pay too, once they write.** With a board, `fp_get` / `fp_check` read lazily
    per library and cost little. But `fp_create`, `fp_approve` and the `replace` path open a
    footprint document, and that loads every library (C4) on a board server as well.
- **No unloading.** The memory stays until the server restarts. A project switch restarts the
  server anyway (C14).
- **Guard rails** (author call):
  - The README states the requirement: about 2 GB extra RAM, 4 GB peak, per api-server that uses
    footprint tools.
  - A setting `COPPERCHAT_FOOTPRINT_TOOLS` (`on` by default, `off` to disable) removes `FP_READ`
    and `FP_WRITES` from every profile, and so prevents any activation on small hosts.
  - An out-of-memory kill of the api-server is handled like any other server death: restart
    from the last save, `KicadUnavailable`.
- **Better fix for later.** A KiCad patch that stops `handleOpenFootprint` from loading every
  library would remove the cost (the spike suggested it). It is not in the 0001–0004 series, so
  it needs an owner decision (§15 Q4).
- **Logging.** On activation CopperChat logs RSS before and after. The first tool result in the
  turn gets a `notes` entry `{kind: "footprint_libraries_loaded", seconds}`, so that a slow call
  is explained.

### 6.2 `FootprintDocument`

`kicad_session/footprint_doc.py` (author call) holds the raw-protobuf helper, which grows out of
the test helper `FootprintDoc`.

- **Entry point.** `async with session.footprint_document(lib_id) as fd:`
- **Locks.**
  - It takes a new `KicadSession._fp_lock`: one footprint document per server (C2).
  - It runs each KiCad call through `session.call` (`_lock`) as today.
  - Writing tools run under the adapter's `EditingService.edit_lock`. Their footprint engine
    enters `session.applying()` (`_apply_lock`) before it takes `_fp_lock`.
  - Read-only tools that open a document (the `fp_get` fallback) hold only `_fp_lock`.
  - **Lock order:** `edit_lock` → `_apply_lock` → `_fp_lock` → `_commit_lock` → `_lock`. None of
    these locks is re-entrant. A read tool must never ask for `_apply_lock` while it holds
    `_fp_lock`, and `session.save()` (which takes `_apply_lock`) is never called inside a
    footprint document block.
- **Methods.** `items()`, `footprint()`, `fields()`, `begin()` / `push()` / `drop()`,
  `create()`, `update()`, `delete()`, `update_footprint(props)` (patch 0002), `save()` and
  `save_copy(target)`.
- **Close always runs** in `finally`, shielded from cancellation like `KicadSession.commit`. A
  failed close is logged and forces a server restart at turn end. The same rule applies to a
  failed save (`DocumentInconsistent`).
- **Reading without a document.** `fp_get`, `fp_check` and `fp_render` (geometry) read through
  batched `GetItemsFromLibrary` (C4) once the handler is active. They open a document only for
  data that is not in the library response; the spike found none for v1.

### 6.3 Seed

- **Seed** (author call): a single pinned stock seed, `Inductor_SMD:L_Bourns-SRN4018` (spike
  3A). Every child item is deleted, and all footprint-level properties are replaced through
  patch 0002, including the mounting type. So one seed serves SMD and THT.
- **Startup check.** The seed's existence is checked when the handler is activated. If it is
  missing, footprint creation is refused with the `seed_missing` error; it names the pinned
  lib_id.
- **The 3D model of the seed is removed** in the same commit (through `definition.items`, as the
  test does), unless `fp_create` supplies a model link.

## 7. Tools

All tools follow the existing conventions:

- each tool is a `ToolSpec` in a new provider `copperchat.kicad_tools.footprints.specs`;
- input schemas are built with `_obj`, use `additionalProperties: false` and give explicit
  `maxLength` / `maxItems` limits;
- errors are `StructuredToolError` (`what` / `why` / `suggestion` / `code` / `details`);
- results are JSON `data`, plus `images` for renders;
- writing tools are two-phase (`edit_spec(prepare, apply)`);
- coordinates are in mm, in footprint coordinates: origin = footprint anchor, x to the right,
  **y down** (KiCad's convention, also used by `kicad-cli`);
- angles are in degrees.

A `lib_id` is `<nickname>:<name>` (`maxLength 256`).

A **name** for new footprints must match `^[A-Za-z0-9][A-Za-z0-9_.+-]{0,99}$`. This rules out
path separators, `..` as a path and spaces. KiCad naming is recommended, e.g.
`L_Bourns_SRN3012BTA`.

### 7.1 Shared geometry model (`FootprintGeometry`)

`fp_get` returns the geometry model, `fp_create` takes (most of) it as input, and `fp_check` works
on it. It is a pure dataclass in `kicad_tools/footprints/model.py`, built from the protobuf
`Footprint` definition.

```jsonc
{
  "lib_id": "OpenDriveHub:L_Bourns_SRN3012BTA",
  "scope": "local" | "project" | "global",   // local = CopperChat's repo library (§5)
  "description": "...", "keywords": "...",
  "mounting": "smd" | "tht" | "unspecified",
  "exclude_from_bom": false, "exclude_from_pos": false, "dnp": false,
  "fields": {"Reference": "L**", "Value": "...", "Datasheet": "...",
             "Footprint source": "...", "needs_user_review": "yes", "...": "..."},
  "pads": [{"number": "1", "type": "smd" | "tht" | "npth" | "connector",
            "shape": "rect" | "roundrect" | "circle" | "oval" | "trapezoid" | "custom",
            "at": [-1.1, 0.0], "rotation": 0, "size": [1.3, 3.5],
            "roundrect_ratio": null, "drill": null | {"size": [0.8, 0.8], "shape": "circle" | "oval"},
            "layers": ["F.Cu", "F.Mask", "F.Paste"]}],
  "graphics": {"F.CrtYd": {"bbox": [-2.0, -2.0, 2.0, 2.0], "closed": true, "items": 1},
               "F.Fab": {...}, "F.SilkS": {...}},      // per-layer summary
  "shapes": [...],            // full list only with detail="full": kind, layer, points/center/size, width
  "models": [{"path": "...", "offset": [0, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]}],
  "texts": [{"kind": "reference" | "value" | "user", "layer": "F.SilkS", "at": [0, -3.0], "size": [1, 1]}],
  "bbox": [xmin, ymin, xmax, ymax],   // pads + shapes, texts excluded
  "geometry_hash": "a1b2c3d4…"   // sha256 over everything manufacturing-relevant (§9.4)
}
```

- The default `detail` is `"summary"` (no `shapes` list). This keeps results small for footprints
  with many graphics (author call).
- **Reference and Value texts** (author call). The seed's mandatory fields cannot be deleted
  (`IDS_IMMUTABLE`, spike Q1), so `fp_create` **moves** them, following KiCad's convention:
  - Reference: on F.SilkS, 1.0 mm above the courtyard;
  - Value: on F.Fab, 1.0 mm below the courtyard;
  - a `${REFERENCE}` user text on F.Fab at the body centre.

  Texts are left out of `bbox`, but they **are** included in the silk checks (§8) and in the
  hash.

### 7.2 `fp_get` (read-only, all session types)

- **Input.** `{lib_id: string, detail?: "summary" | "full"}`.
- **Output.** `FootprintGeometry`, plus:
  - `source` and `review` (`needs_user_review` / `approved`) for local footprints;
  - `notes`, e.g. `footprint_libraries_loaded`.
- **Errors.** `not_found` (with `details.closest` from the existing `_closest_footprints`),
  `invalid_argument` and `library_error`.
- **Path.** Batched `GetItemsFromLibrary` with an LRU cache by lib id. The cache is cleared by
  `invalidate_footprints` (§5).

### 7.3 `fp_list_local` (read-only, all session types)

- **Input.** `{query?: string(≤200), limit?: int(1..200, default 50)}`.
- **Output.**
  - `{library: {nickname, uri, path_rel_repo} | null, footprints: [{name, lib_id, description,
    pad_count, needs_user_review, source, used_by: ["L201", …]}], truncated}`;
  - `used_by` comes from the **open project's** symbol Footprint fields: the API symbol list,
    which `sch_list_symbols` already reads;
  - `library: null` with a note when the repo has no local library yet; this case does not
    activate the handler.
- **Path.** `GetLibraryItems` on the local nickname.

### 7.4 `fp_check` (read-only, all session types)

- **Input.**
  ```jsonc
  {
    "lib_id": "...",
    "symbol"?: "lib:Symbol" | "R201",   // a lib_id or a placed reference: pins to compare
    "expected"?: {                        // datasheet land pattern, for a dimension check
      "pads": [{"number": "1", "at": [x, y], "size": [w, h], "drill"?: d}],
      "pitch"?: number, "courtyard"?: [w, h], "body"?: [w, h],
      "tolerance_mm"?: number             // default 0.01
    },
    "allow_unmatched_pads"?: ["10", "11"] // e.g. shell pads; turns their warning into info
  }
  ```
- **Output.**
  - `{lib_id, passed: bool, counts: {error, warning, info}, findings: [{rule, severity, message,
    pads?, at?, measured?, expected?}], geometry_hash}`;
  - `passed` = no errors.
- **Pure function.** `checks.py` runs on `FootprintGeometry`, plus the symbol's pin list
  (`LibraryService.get_symbol`, or the placed symbol's pins), plus `expected`. No KiCad writes.
- **Stock footprints.** `fp_check` works on stock footprints as well. This is the U202 case: the
  stock DRC0010J footprint checked against the TPS63000 datasheet's `expected`.

The rules are in §8.

### 7.5 `fp_render` (read-only, all session types)

- **Input.**
  `{lib_id, layers?: [string] (default: F.Cu, F.Mask, F.Paste, F.SilkS, F.Fab, F.CrtYd, plus
  B.* when used), grid_mm?: 0.1 | 0.25 | 0.5 | 1.0 (default: auto), labels?: bool (default
  true)}`.
- **Output.**
  - `{image: <filename>, svg: <filename>, scale_px_per_mm, extent_mm: [w, h],
    geometry_hash}`;
  - one PNG `ImageBlock` for the model, which the chat also shows (§11).
- **Pipeline** (`views/footprint_render.py`, author call):
  1. Find the library directory of `lib_id`: the resolved URI from `GetLibraryTable`
     (`substituted = true`) for project rows, or the stock path for global rows.
  2. Run `kicad-cli fp export svg <dir.pretty> --fp=<name> --layers … -o <tmpdir>`.
     - It reuses the existing kicad-cli runner in `kicad_tools/checks.py`: isolated env and
       config home, kill and reap on timeout, one kicad-cli process at a time.
     - Arguments go as an argv list (no shell), with `--fp=<name>` as a single element, so a name
       that starts with `-` is never read as an option.
     - The timeout is 60 s.
     - `<tmpdir>` is a fresh `tempfile.mkdtemp()` that is removed in `finally`.
     - CopperChat **checks that the SVG file exists and is not empty** (C10), because the exit
       code is no proof; otherwise it reports `render_failed`.
  3. CopperChat writes its own wrapper SVG. It is built with an XML API (`xml.etree`), never by
     string concatenation. KiCad's output is parsed and re-serialised without its prolog or
     DOCTYPE, and all label text (pad numbers, names from stock libraries) is escaped by the XML
     API. The wrapper holds:
     - a mm grid: minor lines at `grid_mm`, major lines every 1 mm (every 5 mm above 20 mm
       extent);
     - axis lines through the origin and mm tick labels on two edges;
     - optional dimension labels: the size and centre of pad 1, the pad pitch, and the
       courtyard size;
     - KiCad's SVG embedded as a nested `<svg>` with the same mm `viewBox`.

     The wrapper is our drawing, not a KiCad file, so no rule is broken.
  4. Rasterise the wrapper to PNG with **CairoSVG** (author call). It is a new Python dependency;
     `libcairo2` is already a KiCad runtime library in the base image, which phase 2 verifies.
     The scale is chosen so that 1 mm is at least 40 px, and the longer side is at most
     2000 px. CairoSVG runs with its defaults (no `unsafe=True`, so no external entities and no
     file access) and in `asyncio.to_thread`, so the CPU work never blocks the event loop.
  5. Store both files under `.kicad-agent/renders/` (as `render_view` does).
- **Why the model sees a PNG.** The Claude API takes raster images only. The SVG stays available
  to the user as a zoomable link (§11).

### 7.6 `fp_create` (writing, Schematic + Free)

- **Input.**
  ```jsonc
  {
    "name": "L_Bourns_SRN3012BTA",
    "description": "Bourns SRN3012BTA shielded power inductor, 3.0x3.0 mm",
    "keywords"?: "inductor SRN3012",
    "mounting": "smd" | "tht",
    "pads": [ /* ≤ 512, same shape as FootprintGeometry.pads; layers default by type */ ],
    "courtyard"?: {"rect": [w, h], "center"?: [x, y]} | {"margin": 0.25},  // default margin 0.25 around pads + body
    "body": {"rect": [w, h], "center"?: [x, y]},          // F.Fab outline (mandatory: it documents the part outline)
    "silkscreen"?: "auto" | {"segments": [[[x1, y1], [x2, y2]], …]},  // ≤ 256 segments; auto: body outline clipped 0.2 mm clear of pads
    "pin1_marker"?: "auto" | "none",                      // auto: silk + fab marker at pad 1
    "fields"?: {"Datasheet": "...", "...": "..."},        // ≤ 16 extra fields, names ≤ 64, values ≤ 1024 chars; Reference/Value are set by the tool
    "model"?: {"path": "${KICAD<n>_3DMODEL_DIR}/…" | "${KIPRJMOD}/…", "offset"?, "rotation"?, "scale"?},
    "source": {                                           // MANDATORY
      "document": "docs/parts/SRN3012BTA-2R2M/datasheet.pdf" | "https://…",
      "location": "p.2, 'Recommended layout'",           // page/figure/table, non-empty
      "mpn"?: "SRN3012BTA-2R2M"
    },
    "symbol"?: "Inductor:L",   // pins used for the post-create fp_check
    "replace"?: false          // true only for an existing *local* footprint (§9.5)
  }
  ```
- **prepare** (read-only, may refuse; nothing is written yet):
  - Validate the schema and the name.
  - **The name must be free** in the local library unless `replace` is set:
    - with a row, this is checked through `GetLibraryItems`;
    - **without a row** in this project, the API cannot list the library, so prepare uses
      `local_names_on_disk()` (§5). The shared directory may already hold another project's
      footprint of that name.
    - `apply` checks again through the API right before S2 (§9.3). This closes the gap from a row
      that was missing at prepare time and from edits in the KiCad GUI. `SaveCopyOfDocument`
      would otherwise overwrite silently (C1).
  - Validate `source`:
    - A repo path must be **relative**. It is resolved through `repo_context`'s file resolution,
      which applies the repo root, the block rules (`.git`, env files, `.kicad-agent`, …) and the
      symlink policy, and it must exist as a regular file. Absolute paths and blocked paths are
      refused with the same `invalid_argument` message as missing files, so the check cannot be
      used to probe for files.
    - A URL must be http(s).
    - `location` must be non-empty.
  - Validate the model path. It is a link only, so the file need not exist; if it is missing, a
    note says so. The rules:
    - it must start with `${KIPRJMOD}/` or `${KICAD<n>_3DMODEL_DIR}/` (a fixed list of variable
      names);
    - no `..` segment;
    - the suffix is `.step`, `.stp`, `.wrl` or `.wrz`.
  - Build the target `FootprintGeometry` (auto courtyard, silk, pin-1 marker) and run `fp_check`
    on it, with `symbol` if given. Any **error** refuses the call (`check_failed`, with
    `details.findings`); warnings pass through to the result.
  - Resolve the local library. If it does not exist yet, the plan includes the row (§5).
  - Set `Logical(changes=…)` (§12).
- **apply** — stages and compensation are in §9.3.
- **Output.** `edit`-style result:
  `{created: [lib_id], file: "footprints/OpenDriveHub.pretty/L_Bourns_SRN3012BTA.kicad_mod",
  library_created: bool, check: {…fp_check on the read-back…}, geometry: {…summary…},
  needs_user_review: true, logical_changes, budget, notes}`.
- **The suggestion text** tells the agent to call `fp_render` and present the render, the check
  and the source to the user for approval.

### 7.7 `fp_approve` (writing, Schematic + Free) (author call)

The owner decided that the agent records the approval after the user says ok in chat (flow A).
The author chose a separate tool over a generic field update, because the server can then guard
the approval:

- **Input.** `{lib_id, geometry_hash: string, user_quote: string(≤300)}`. `user_quote` is the
  user's own words of approval, copied by the agent.
- **Guards.** The call is refused with `approval_not_allowed` unless all of these hold:
  1. `lib_id` is a local footprint with `needs_user_review = yes`.
  2. `geometry_hash` equals the current read-back hash. This proves that the user approved this
     geometry and not an earlier version.
  3. CopperChat produced an `fp_render` **and** an `fp_check` result for that hash in this
     conversation. The server records these in a small per-conversation table
     `footprint_reviews(conversation_id, lib_id, geometry_hash, run_id, rendered_at,
     checked_at)`, created by a new migration (`00NN_footprints.sql`).
  4. After both `rendered_at` and `checked_at`, the conversation has a **human** user message:
     one typed by the user. Server-generated user-role messages do not count: the plan-execute
     message (`plans/service.py`), session-close or handover prompts, notices and other entries
     with origin, plan or event metadata.
  5. `user_quote` is a substring of that human message, compared after whitespace
     normalisation. The agent cannot make up the words.
  6. No plan is `running` in this conversation, and the turn was started by that human message.
     So `fp_approve` cannot be called inside plan execution or an automated turn.
  7. Error-free `fp_check` on the current read-back.

  Guards 3–6 are the strongest server-side evidence available that the user saw this geometry
  and approved it. They still cannot judge how clear the approval was (§15 Q1).
- **Effect** (one commit on the footprint document):
  - `needs_user_review` → `no`;
  - a new field `User review` = `approved <YYYY-MM-DD>, conversation <id8>, geometry <hash8>`;
  - `user_quote` is stored in the change log, not in the KiCad file.
- **Budget.** 1 logical change.
- **No "un-approve" tool.** Any later change through `fp_create replace=true` sets
  `needs_user_review = yes` again and drops `User review`. History undo restores the earlier
  state.
- **A safer variant for later.** An "Approve" button on the render card in the chat would record
  the approval without the agent. See §15 Q1.

### 7.8 `fp_delete` (writing, Schematic + Free)

- **Input.** `{lib_id}`. Only footprints in the local library can be deleted.
- **prepare** refuses in these cases:
  - `not_found`;
  - `not_local`;
  - `in_use`: a symbol of the open project references the footprint (refs in `details`).
- **Other projects of the repo.** CopperChat cannot ask their KiCad sessions (C14). Instead it
  does a read-only byte search for the exact lib-id string in the `*.kicad_sch` / `*.kicad_pcb`
  files of the other project directories under the repo root. A hit gives a warning note
  `possibly_in_use_elsewhere`, not a refusal (author call). No file is parsed or written.
- **apply.** `DeleteLibraryItem` (patch 0003). Afterwards CopperChat reads back that the
  footprint is gone and invalidates the cache.
- **Budget.** 1 logical change.
- **Undo** is through history (§10).

### 7.9 `sch_set_fields` / `sch_place_symbol`: footprint existence

`footprint_notes` (`kicad_tools/edit/specs_place.py`) changes as follows:

- If the footprint's nickname is the **local** library (`footprint_notes` runs in `prepare`, so
  it writes nothing):
  1. **The row exists:** activate the handler (§6.1) and check existence through the API. An
     unknown name → `not_found`, with `closest` from the local library. If the footprint has
     `needs_user_review = yes`, add the note `footprint_needs_review`. The assignment is allowed:
     the user may approve later.
  2. **The row is missing:** check existence with `local_names_on_disk()` (§5) and set
     `ToolChangeSet.library_row = LibraryRowStage(nickname, uri)`. The review flag cannot be
     read yet, so the note `footprint_review_unknown` is added.
- Otherwise, if `footprint_libraries_ready()` is true, the existing behaviour applies.
- Otherwise the existing `footprint_unverified` note stays. Its text changes from "this project
  has no board" to "footprint libraries are not loaded (no board, no footprint tool used)".

**The row stage runs outside the schematic engine** (author call). `apply_change_set` and the
schematic `Stage` stay unchanged. The adapter (`agent/sdk_tools.py`, `_two_phase_steps`) gains
one generic step: after the turn snapshot and before `spec.apply`, it runs `cs.library_row` when
it is set. That step:

1. calls `AddLibraryTableEntry`;
2. reads the row back and checks through the API that the footprint exists;
3. on a mismatch, deletes the row again and raises `not_found`.

If `spec.apply` then fails, the adapter compensates with `DeleteLibraryTableEntry`. The
`create_library` flag is never set here, because the directory already exists.

- **Budget.** The row costs 0 logical changes.
- **Result.** `side_effects: ["fp_lib_table_row_added"]`.
- **The same step serves `fp_create`'s S1.** The footprint engine calls it directly.

## 8. `fp_check` rules (author call)

These thresholds are fixed and board-agnostic, with IPC-7351 nominal values where they apply. SP4
replaces them with the project's design rules. Severity: **error** blocks `fp_create` and
`fp_approve`; **warning** is reported and must be mentioned to the user; **info** is reported
only.

| Rule id | Check | Severity |
|---|---|---|
| `pin_without_pad` | A symbol pin number (with `symbol`) has no pad. | error |
| `pad_without_pin` | A numbered copper pad has no symbol pin. | warning (info if listed in `allow_unmatched_pads`). J402: shell pads 10/11 are reported, not failed |
| `pad_count_summary` | `n pins / m pads / k unnumbered`, always included with `symbol`. | info |
| `duplicate_pad_number` | Several pads share a number (an EP split, a multi-pad pin). | info |
| `expected_mismatch` | A pad position, size, drill, pitch, courtyard or body differs from `expected` by more than the tolerance. | error |
| `expected_missing_pad` | A pad from `expected` is missing, or the footprint has an extra pad. | error |
| `courtyard_missing` | No F.CrtYd (or B.CrtYd for bottom-only parts), or the outline is not closed. | error |
| `courtyard_not_enclosing` | Copper or the body (F.Fab) lies outside the courtyard. | error |
| `courtyard_margin` | The gap between the courtyard and the copper/body is below 0.25 mm (below 0.1 mm for packages ≤ 1.0 mm, i.e. 0402 and smaller). | warning |
| `fab_missing` | No F.Fab outline. | warning |
| `silk_on_pad` | The silk clearance to the copper is below 0.1 mm (spike threshold). | warning |
| `silk_outside_courtyard` | Silk lies outside the courtyard. | warning |
| `pin1_marker_missing` | No silk or fab mark next to pad 1, for footprints with more than 2 pads, or with 2 pads and polarity (from `symbol`). | warning |
| `mask_missing` | An SMD pad has no F.Mask. | error |
| `paste_missing` | An SMD pad has no F.Paste. | warning |
| `paste_on_tht` | A THT pad has paste. | warning |
| `mounting_mismatch` | `attributes.mounting` contradicts the pad types (SMD with only THT pads, or the other way round). | error |
| `pad_too_small` | A pad dimension is below 0.15 mm. | warning |
| `pad_gap_small` | The copper gap between pads of different numbers is below 0.15 mm (warning); below 0.1 mm it is an error. | warning / error |
| `drill_small` | A drill is below 0.2 mm (error); 0.2–0.3 mm is a warning. | error / warning |
| `annular_ring_small` | The annular ring is below 0.15 mm. | warning |
| `source_missing` | A local footprint has no `Footprint source` field. | error (local) / n/a (stock) |
| `needs_user_review` | A local footprint is not approved yet. | info |
| `approved_geometry_changed` | `User review` exists but its hash no longer matches. | warning |

The geometry is computed in `checks.py` from the pad shapes: rect, roundrect and oval as exact
polygons, circle exact, custom by its polygon. Graphics are taken as stroke centre lines plus
half the stroke width. The checks use **no shapely or other new library** (author call). The
shapes are simple, and the existing `edit/geometry.py` style is plain Python.

**Cost bounds** (author call). Stock footprints can be large (BGAs, logos), so `fp_check` must
not stall the server:

- It runs in `asyncio.to_thread`.
- Pair checks (`pad_gap_small`, `silk_on_pad`) first filter candidate pairs by bounding box,
  with sort-and-sweep on x, so they are not all-pairs.
- Above 2,000 pads or 5,000 shapes, the exact pair checks are skipped. An info finding
  `too_complex_partial_check` then names the skipped rules. The other rules still run.

## 9. Write path, atomicity and compensation

### 9.1 Adapter integration

The writing footprint tools use `edit_spec(prepare, apply)` and go through the same
`_run_two_phase` steps as the schematic tools:

1. read-only check;
2. edit lock;
3. halted check;
4. gate;
5. `prepare`;
6. budget;
7. turn snapshot (`on_mutation`);
8. `apply`;
9. gate record;
10. save.

**`FootprintChangeSet(ChangeSet)`** (author call) is a subclass with `sheet=None` and empty
`stages`. Its payload is a list of `FpStage` objects, applied by
`kicad_tools/footprints/engine.py`. The schematic engine is not touched.

**`DocKind` stays `"sch" | "pcb"`.** Footprint tools return `changed_docs = set()`. `DocKind`
also drives the turn-end autosave (`history/hooks.py`), the save listener, `publish_view` and
`editing/hooks.py`; a new kind would make them call `session.save()` with a footprint kind that
`_document_type` maps to a PCB. Instead:

- `ToolResult` gains a field `library_changed: set[str]` (nicknames).
- The adapter passes it to a new `ToolContext.on_library_changed` callback. That callback:
  - re-records the hashes of the tracked library set (§10), so CopperChat's own writes are never
    taken for external changes;
  - calls `invalidate_footprints`;
  - marks the turn as changed for the UI change list.
- Footprint stages save inside `apply` (C9), so the adapter's save step has nothing to do for
  them.

### 9.2 Read-back verification

After every stage that writes, CopperChat reads back through the API:

- **Row added:** `GetLibraryTable` contains the row, and `GetLibraryStatuses` reports the library
  as `LLS_LOADED`.
- **Footprint written:** the footprint is reopened, or read with `GetItemsFromLibrary`, and the
  resulting `FootprintGeometry` must equal the target geometry from `prepare` within 1 µm, with
  the fields and properties as set.
- **Deleted:** the footprint is gone from `GetLibraryItems`.

If the read-back does not match, that is `verification_failed`, followed by compensation.

### 9.3 Stages and compensation ("tool error = nothing written")

`fp_create` runs up to three stages. If a later stage fails, CopperChat compensates the earlier
ones in reverse order before it raises the error:

| Stage | Writes | Compensation |
|---|---|---|
| S1 `ensure_library` (only if no local row) | `AddLibraryTableEntry(create_library)`: the row plus an empty `.pretty` directory | `DeleteLibraryTableEntry`. Then the `.pretty` directory is removed with `rmdir`, but only if it is **empty and was created by this call** (author call: this is the only file-system operation, and it writes no content). |
| S2 `copy_seed` | `SaveCopyOfDocument(seed → lib:name)`: a new `.kicad_mod`. With `replace`, the API pre-image was read in `prepare` (§9.5). | `DeleteLibraryItem` (patch 0003). With `replace`, the old footprint is rebuilt from the pre-image (§9.5). |
| S3 `rebuild` | Open the copy, then one commit: delete all children, create pads, graphics and fields, update footprint properties (patch 0002), remove the seed model or set the link; then `SaveDocument`. | A failure before save → `drop` the commit, then compensate S2. A failure after save → compensate S2. |

**Before S2** (author call), the engine re-checks the name through the API
(`GetLibraryItems`), now that the row exists. If the name is taken, it raises `already_exists`
and compensates S1. This prevents a silent overwrite.

**If compensation itself fails**, CopperChat raises `document_inconsistent`, as the schematic
engine does. `_recover_inconsistent` only reopens the session from the last save; it **never
restores a snapshot automatically** (owner rule). So the leftover stays on disk:

- a footprint saved by S2/S3, or a table row added by S1;
- the error and the turn-end notice name it exactly (lib id or row);
- the user removes it with History → undo of the turn (§10 makes that work), or the agent removes
  it with `fp_delete` in a later turn.

This one case breaks "tool error = nothing written", and the notice says so; it is never
silent.

**Cancellation.** The compensation runs shielded, like `KicadSession.commit` cleanup.

`fp_delete` and `fp_approve` have a single stage. `fp_approve` = one commit + save; a failure
before save is a `drop`.

### 9.4 Geometry hash

`geometry_hash` is a sha256 over a canonical JSON of **everything that changes what is
manufactured or placed**. Coordinates are rounded to 1 nm, and lists are sorted. The hash
covers:

- every pad with all its properties: number, type, shape, position, rotation, size, drill,
  layers, per-pad clearance, mask and paste margins, zone connection and thermal settings;
- all shapes on the Cu, Mask, Paste, SilkS, Fab and CrtYd layers;
- the positions, sizes and layers of all texts;
- the footprint-level properties from patch 0002: mounting type, exclude-from-BOM/position,
  clearance/mask/paste/zone overrides, net ties, private layers and jumpers;
- the 3D model links.

Left out are only the description, keywords and field **values**. So editing metadata does not
invalidate an approval, but every manufacturing-relevant change does. `fp_render`, `fp_check`,
`fp_create` and `fp_approve` all report the hash.

### 9.5 `replace`

`fp_create(replace=true)` is allowed only for an existing **local** footprint. It counts as
deletion plus creation in one call:

- `prepare` reads the full pre-image as **API data**: the raw protobuf items (pads, shapes,
  texts, fields) and the `FootprintInstance` definition. CopperChat never keeps file bytes for
  this.
- S2 uses the **seed** again, so the result is built from scratch.
- **Compensation is best effort.**
  - On failure, CopperChat rebuilds the pre-image through the same S3 code path and compares
    the read-back with the pre-image: the geometry hash plus fields.
  - Things the API does not expose may be lost (e.g. groups). Any difference raises
    `document_inconsistent` and names the footprint (§9.3).
  - The turn snapshot holds the exact old bytes, so History → undo is the backstop.
- The result has `needs_user_review = yes`, and any `User review` field is dropped.

## 10. Undo, history and snapshots

C12 is extended in `history/store.py` (author call for the mechanism).

**The core problem.** The local library is shared by every project of the repo, but history is
per project (`HistoryStore` per `agent_dir(project)`, hashes per `project_id`). Restore must
therefore never undo another project's footprint work.

- **`document_files(project)` → `tracked_files(project, repo_root)`.** The tracked set is:
  - the existing project files;
  - the project's `fp-lib-table` (it may be absent);
  - every `*.kicad_mod` in the **local library directory** `<root>/footprints/<nick>.pretty`
    (§5).

  The library directory is derived (§5), not read from the table. So it is tracked even before
  this project has a row, or before the directory exists, and the very first `fp_create` is
  undoable. The walk:
  - takes regular files only (`lstat`);
  - skips symlinks, as `document_files` does today;
  - never tracks anything outside the repo root.

  While `repo_policy` is `transient` (§5), the library part is left out of snapshots, and
  restore does not touch it.
- **Paths.** `relPath` stays **project-relative**, as today. Library files get `../` prefixes,
  e.g. `../../footprints/X.pretty/L.kicad_mod`. Existing manifests, the `document_hashes.rel_path`
  keys and `history_snapshots.files` keep their meaning, so no data migration is needed.
  Restore's `_inside` check changes from "inside the project dir" to "inside the repo root",
  with no symlink component, and for `../` entries "inside the local library directory".
- **Manifest v2** adds:
  - `version: 2`;
  - `repoRootRel`: the project directory relative to the root;
  - `library`: `{dir, existed, files}`;
  - `table`: `{existed}`.

  Restore refuses the library part, and says so, when `repoRootRel` or the derived library
  directory differs from today's (the repo was moved or renamed). The project files are restored
  as usual in that case.
- **Restoring the library is scoped to this project's own changes.** The change log knows which
  footprints this project's tool calls created, replaced, approved or deleted after the
  snapshot. Only those are restored, and only when the file's current hash still equals the hash
  this project recorded right after its own last write. If someone changed the file since
  (another project, the KiCad GUI), it is left alone. Per file:
  - created here after the snapshot → deleted;
  - changed or deleted here → the snapshot bytes are copied back;
  - changed elsewhere since → **skipped**, and listed in the restore notice as "changed outside
    this project, not restored".

  Library files that this project never touched are never modified by its restore. These are
  restore-time file operations on snapshot bytes that KiCad wrote, like today's copy-back of
  `.kicad_sch` files; CopperChat never authors content.
- **The table and the library directory on restore** (v2 manifests only):
  - If the v2 snapshot had **no** `fp-lib-table` and the current table holds only rows that
    CopperChat added after the snapshot (change log `fp_lib_table_row_added`), the table is
    removed. Otherwise only those rows are removed, through the API after the reopen. Any other
    row stays.
  - An empty library directory that this project created after the snapshot is removed with a
    non-recursive `rmdir`.
  - **v1 manifests** (older snapshots, without the table or the library) **never** delete
    anything. They restore project files only, exactly as today. So restoring an old snapshot
    cannot remove a hand-made `fp-lib-table`.
- **Validation of every delete.** A file is deleted only if all of these hold:
  - it is a regular file (`lstat`);
  - it lies directly in the derived local library directory;
  - its name ends in `.kicad_mod`;
  - no path component is a symlink.

  A `library.dir` in the manifest that differs from the derived directory is refused, so an
  edited manifest cannot steer deletes elsewhere.
- **After a restore.** Restore already closes and reopens the session (`restore.py`), which
  reloads the tables and libraries. `invalidate_footprints` is called.
- **Own writes are not external changes.** After every footprint stage, `on_library_changed`
  (§9.1) re-records the hashes of the tracked library files. So the next `on_turn_start` does not
  report CopperChat's own `fp_create` as an external edit.
- **Real external changes** are detected by `on_turn_start`, which hashes the tracked set. There
  are two cases:
  - A change in **project files** (`.kicad_sch`, `.kicad_pcb`, `.kicad_pro`, the project's own
    `fp-lib-table`) keeps today's reaction: reload, notice, plan pause.
  - A change **only in library files** (another project of the repo, the KiCad GUI) does not
    reload the session and does not pause a plan. It calls `ReloadLibrary(nick)` +
    `wait_for_libraries`, then `invalidate_footprints`, and posts a short notice naming the
    changed footprints. A reload would drop `footprints_active` and cost 3 s and 1.7 GB again
    for nothing.
- **Snapshot size: content-addressed library blobs.** Snapshots are taken often (`HISTORY_KEEP`
  50 turn snapshots plus `STEP_KEEP` 100 step checkpoints), and there is one history per project
  of the repo, so copying the whole library each time is too expensive. Instead:
  - library files are stored once per content, as `.kicad-agent/history/blobs/<sha256>`;
  - the manifest references them by sha;
  - an unchanged file costs nothing per snapshot;
  - pruning a snapshot removes blobs that no manifest references any more;
  - hashing reuses the hashes recorded by `on_library_changed`, so only files that changed are
    read again.
- **Change log.** The change log (`changes`) gets these kinds for the UI change list, each with
  the lib_id and the geometry hash:
  - `footprint_created`;
  - `footprint_replaced`;
  - `footprint_deleted`;
  - `footprint_approved`;
  - `fp_lib_table_row_added`.

## 11. Showing footprints in chat

- **Rendering.** `fp_render` returns a PNG `ImageBlock`. The run engine already stores tool
  images (`.kicad-agent/images/…`) and shows them in the chat row. No new frontend component is
  needed for v1.
- **The SVG link.** The existing renders route (`web/routes_views.py`) accepts only
  `^[0-9a-f]{32}\.png$` and always sends `image/png`, so the SVG needs its own route (author
  call). The route is `GET /api/projects/<id>/renders/<uuid>.svg`:
  - its name pattern is `^[0-9a-f]{32}\.svg$`, and the names are server-generated uuids, never
    derived from footprint names;
  - it sends these headers:
    - `Content-Type: image/svg+xml`;
    - `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox`;
    - `X-Content-Type-Options: nosniff`;
    - `Content-Disposition: inline; filename="<lib>_<name>.svg"` (sanitised).

  The path stays under the `/renders/` prefix, which the markdown policy already allows (see
  the `web/security.py` docstring). The agent may link the SVG in markdown so the user can
  zoom. Markdown image rendering of SVG stays an `<img>` (no script execution). The SVG is
  re-serialised by CopperChat (§7.5), and the CSP is defence in depth. P2 includes a route test
  for the headers and the name pattern.
- **The approval message** that the skill prescribes contains:
  - the render;
  - the `fp_check` summary with every warning;
  - the source (document + location);
  - the key dimensions, measured from the read-back and not copied from the input;
  - the explicit question "Approve this footprint?".

## 12. Logical-change budget and plan gate

Footprint tools count against the same per-turn budget (`EditingSettings.change_budget`, default
20) and the same plan gate (`plans/gate.py`) as the schematic tools.

| Tool | `Logical.changes` (author call) |
|---|---|
| `fp_create` (new) | 5. A footprint is one unit for the user, but it carries many items and needs a review. At most 4 new footprints fit in a turn without a plan. |
| `fp_create(replace)` | 5 |
| `fp_approve` | 1 |
| `fp_delete` | 1. It is not counted against `MAX_PREEXISTING_SYMBOL_DELETES`. |
| S1 library row (inside `fp_create` / `sch_set_fields`) | 0 (infrastructure) |
| read-only `fp_*` | 0 |

- **Plans.** A plan step may contain `fp_create` and `fp_delete`. `plan_propose` step kinds are
  free text today, so nothing changes there. `fp_approve` is **never** allowed while a plan is
  running (§7.7 guard 6). Approval stays a separate turn after the user's answer.
- **Turn snapshot.** The snapshot is taken on the first mutation (§9.1). An `fp_create` refused in
  `prepare` takes no snapshot, as today.

## 13. Session types, tool profiles and skills

### 13.1 Profiles (`agent/profiles.py`)

- New group `FP_READ = {fp_get, fp_list_local, fp_check, fp_render}`. It is added to
  `requirements`, `schematic`, `board` and `free`. The owner decided this for `fp_get` and
  `fp_render`; including `fp_check` and `fp_list_local` is an author call, because both are
  read-only.
- New group `FP_WRITES = {fp_create, fp_approve, fp_delete}`. It is added to `schematic` and
  `free` (owner). `board` gets it with SP4.
- Requirements sessions stay read-only. Note that `fp_get` and the others can activate the handler
  there (memory, §6.1), which is acceptable because it happens only on use.
- `copperchat-tools` and the per-session skills (`session-schematic`) list the new tools.

### 13.2 Skills

**New built-in skill `kicad-footprints`** (author call: its own skill rather than more text in
`kicad-schematic-editing`). It covers:

1. **Library rule.** Owner decision, in this order:
   1. **KiCad stock.**
   2. **Manufacturer-provided.**
   3. **Third-party.**
   4. **Handmade (`fp_create`) last.**

   Whatever the origin, a footprint is used only after it has been **checked against the
   datasheet land pattern** with `fp_check(expected=…)`. A stock footprint that differs is not
   used silently: the agent reports the difference.
2. **No invented dimensions.**
   - Every number in `fp_create` comes from the cited source: the datasheet's recommended land
     pattern, preferred over the package drawing.
   - Missing values (e.g. no courtyard given) use the documented defaults (§7.6), and the
     approval message says so.
   - If the land pattern is not in the datasheet, the agent asks the user and does not derive
     pads from the package size. IPC generation is a later feature.
3. **The procedure:**
   1. `parts_get_notes`;
   2. pick the land pattern;
   3. `fp_create` with `symbol`;
   4. `fp_render`;
   5. present the result (§11) and stop;
   6. `fp_approve` only in a later turn, after an explicit ok;
   7. assign with `sch_set_fields`.
4. **Checking existing footprints** (the J402 and U202 pattern): use `fp_check` with `symbol` and
   `expected`. Explain unmatched shell or mounting pads to the user.
5. **Memory and time.** The first footprint tool in a session can take a few seconds.

**`kicad-schematic-editing`:**

- Replace the passage "creating library symbols or footprints is not something you can do" with:
  "creating footprints: see the `kicad-footprints` skill; symbols cannot be created".
- Explain the new notes `footprint_needs_review` and `footprint_libraries_loaded`.
- Update the boardless "unverified" wording.

**`part-notes`:**

- Rule 10 also allows a **local** footprint (`<repo-nick>:<name>`) in the `KiCad footprint` cell
  (the validator regex already accepts it).
- New optional section `## Land pattern`:
  - It is a table `| Pad | X (mm) | Y (mm) | W (mm) | H (mm) | Drill (mm) |` plus courtyard and
    body, each row cited `[D1 p.N]`.
  - The researcher fills it when there is no exact stock match.
  - It is optional, so `REQUIRED_SECTIONS` does not change and existing notes stay valid. The
    research agent's prompt mentions it.
  - `fp_create.source.document` is usually the `docs/parts/<MPN>/datasheet.pdf` from the
    research.

**`part-preferences`:** no change. `preferred_libraries` may name the local nickname.

## 14. Decisions

### 14.1 Decided by the owner (2026-11-15 … 2026-11-17)

- API only, with KiCad patches (B) for the library table, footprint properties and delete. No
  hand-written KiCad files.
- One library per repo at the repo root, referenced through a `${KIPRJMOD}`-relative project row.
- Approval flow A, in chat:
  - the agent shows the render with a mm grid, the check results and the source;
  - the user says ok;
  - the agent records the approval;
  - `needs_user_review` stays set until then.
- v1 tools: `fp_get` (any footprint, no board needed), `fp_create` (explicit pads, mandatory
  source), `fp_check`, `fp_render` (SVG + mm grid), `fp_delete` and `fp_list_local` (local
  only), and the `sch_set_fields` existence check for local libraries.
- IPC-7351 templates and real 3D models come later; a STEP path link is allowed in v1.
- Undo and history cover the library and `fp-lib-table`.
- The ~1.7 GB memory cost may only hit sessions that use footprint tools.
- `fp_get` / `fp_render` are in all session types; the writing tools are in Schematic and Free
  sessions (Board later).
- The interactive KiCanvas footprint viewer is a later option.
- Footprints come before the board (SP4).

### 14.2 Calls made by the author (for review)

- **Library (§5).**
  - The nickname is the repo directory name, with a `_local` suffix on a global collision, the
    same in every project.
  - The local library is exactly that one directory, and every other library is read-only.
  - A row pointing at it under another nickname is a `library_conflict`.
  - Writes are refused while the repo root is transient.
  - The row is created lazily by the first write.
- **Footprint access (§6).**
  - A `footprints_active` flag, activation by opening a pinned stock footprint, and no
    unloading.
  - The `COPPERCHAT_FOOTPRINT_TOOLS` switch and the documented RAM need.
  - The lock order.
- **Seed (§6.3).** A single pinned seed `Inductor_SMD:L_Bourns-SRN4018` for SMD and THT.
- **Geometry model (§7.1).** The `FootprintGeometry` model with `detail = summary` as the
  default, and the placement of the Reference/Value texts.
- **Rendering (§7.5, §11).**
  - CopperChat's own wrapper SVG with grid, axes and pad labels, built with an XML API.
  - The existing kicad-cli runner.
  - Rasterising to PNG with CairoSVG (a new dependency) in a thread.
  - A separate SVG route with a sandbox CSP.
- **Approval (§7.7).** A separate `fp_approve` tool with these guards:
  - the geometry hash;
  - a render and a check followed by a **human** user message;
  - `user_quote` must be a substring of that message;
  - not during plan execution.

  It writes a `User review` field.
- **Deleting (§7.8).** `fp_delete` is refused if the footprint is used in the open project; it
  only warns when it may be used in other projects of the repo.
- **`sch_set_fields` (§7.9).** The row stage is a generic adapter step, outside the schematic
  engine.
- **Checks (§8).**
  - The `fp_check` rules, thresholds and severities.
  - No new geometry library.
  - Bounding-box prefiltering and complexity caps.
- **Write path (§9).**
  - `FootprintChangeSet`, its stages and compensation.
  - An API name re-check before S2.
  - `rmdir` of an empty library directory created by the failing call.
  - A `library_changed` result field instead of a new `DocKind`.
- **Geometry hash (§9.4).** It covers everything manufacturing-relevant and leaves out only
  text metadata.
- **`replace` (§9.5).** It rebuilds from the seed; its compensation is best effort from the API
  pre-image, with history as the backstop.
- **History (§10).**
  - Project-relative manifest paths, with `../` for library files.
  - Manifest v2.
  - Library restore scoped to this project's own changes, with a hash check.
  - No deletes from v1 manifests.
  - A library-only external change reloads libraries, not the session.
  - Content-addressed library blobs.
- **Budget (§12).** `fp_create` costs 5, `fp_approve` / `fp_delete` 1, the row 0.
- **Profiles and skills (§13).**
  - `fp_check` / `fp_list_local` in all profiles.
  - A separate `kicad-footprints` skill.
  - An optional `## Land pattern` section in part notes.

## 15. Open questions for the owner

1. **How the approval is recorded.** With an agent tool (`fp_approve`, guarded by "human reply + quote +
   geometry hash"), the agent could still approve after a vague "looks fine" from the user. An
   "Approve" button on the render card would make the approval a direct user action.
   *Recommendation:* ship `fp_approve` in v1 as designed, and add the button with the frontend
   work of SP4.
2. **Manufacturer and third-party footprints in v1.** The library rule ranks them before
   handmade footprints, but there is no API to import a `.kicad_mod`, and CopperChat must not
   write one. *Recommendation:* in v1 the user adds such a library themselves, e.g. in the KiCad
   GUI or by committing the vendor `.pretty`; CopperChat reads and checks it (`fp_get` /
   `fp_check`). A later `fp_import` tool would add a project row through the API for an uploaded
   `.pretty`.
3. **Budget weight of `fp_create`.** At 5 logical changes, at most 4 new footprints fit in a turn
   without a plan. *Recommendation:* keep 5, so that a batch of new footprints goes through a
   plan.
4. **A fifth KiCad patch for memory.** Every footprint document open loads all ~156 footprint
   libraries: +1.7 GB RSS, up to 4 GB peak, also on board servers once something is written. A
   small patch to `handleOpenFootprint` (load only the footprint's own library) would remove
   this. It would be carried like 0001–0004 and offered upstream through the owner.
   *Recommendation:* ship v1 without it, using the switch and the documented RAM need (§6.1).
   Do the patch together with the next KiCad bump, if PROD memory turns out to be tight.

## 16. Testing

### 16.1 Unit tests (`backend/tests/unit/footprints/`, no KiCad)

- `checks.py`: one test per rule and severity on synthetic `FootprintGeometry`. This includes:
  - the J402 case (9 pins, 11 pads) → a `pad_without_pin` warning for 10/11, or info with
    `allow_unmatched_pads`;
  - a DRC0010J-like 10 + EP case with `expected` mismatches → `expected_mismatch`.
- `model.py`: protobuf → geometry, using recorded protobuf fixtures. The canonical
  `geometry_hash` is stable. It changes on every manufacturing-relevant change, such as a paste
  margin, a mounting attribute or a model link, but not on a description or field value.
- `checks.py` cost bounds: a synthetic 2,500-pad footprint → `too_complex_partial_check`, and the
  run finishes within a time bound.
- Schema and `prepare` validation:
  - the name regex;
  - the source rules: absolute path, `..`, blocked path (`.env`, `.git/…`), missing file and
    empty location all give the same error;
  - the model path rules;
  - `replace` on a non-local footprint;
  - the overwrite refusal, also from `local_names_on_disk()` when the row is missing.
- Nickname and URI derivation (§5): nested, root and non-git projects; a global collision; the
  same result for two projects of one repo; `library_conflict` for a foreign nickname on the
  directory; `repo_root_unknown` while the policy is transient.
- `fp_approve` guards:
  - same turn, wrong hash, no render, no check, check errors;
  - a server-generated user message in between (plan execute, session close, notice);
  - a `user_quote` that is not in the human message;
  - a call while a plan is running.
- History (`tmp_path`, no KiCad):
  - `tracked_files`, including the derived library directory before it exists, and symlinked
    files and directories being skipped;
  - manifest v2 read/write and v1 compatibility;
  - **a v1 restore with an existing `fp-lib-table` deletes nothing**;
  - restore deletes only this project's own footprints created after the snapshot;
  - a file changed by "another project" after this project's write is skipped and reported;
  - a manifest with a forged `library.dir` or a symlinked component deletes nothing;
  - blobs are deduplicated across snapshots, and prune removes unreferenced blobs.
- Adapter: `library_changed` re-records hashes, so no external change is reported on the next
  turn start; the `library_row` pre-stage is compensated when `spec.apply` fails.
- `LibraryService.invalidate_footprints` clears `_fp_scopes`, `_fp_libs` and the LRU.
- Render wrapper: the grid SVG for a given `viewBox` (pure string/XML); rasterisation with
  CairoSVG produces a PNG of the expected size; a missing or empty kicad-cli output →
  `render_failed`.
- Profiles: `FP_READ` is in all profiles, `FP_WRITES` only in schematic/free; Free stays a
  superset.
- Budget: the costs from §12 reach `BudgetGate`.

### 16.2 KiCad integration tests (`pytest -m kicad`, `backend/tests/kicad/footprints/`)

They use the existing fixtures (`blank_session`, `blank_nopcb_session`) plus a new fixture
**`repo_session`**: a synthetic git repo with `hardware/main` and `hardware/io` projects, created
by KiCad, as in spike 3A.

- **L201 golden.**
  - `fp_create` with the owner's numbers in a boardless project.
  - Compare `fp_get` with the golden file `fixtures/golden/fp_get_L201.json` (geometry, fields,
    properties).
  - The file is at `<repo>/footprints/<nick>.pretty/L_Bourns_SRN3012BTA.kicad_mod`.
  - The project row URI is `${KIPRJMOD}/../../footprints/<nick>.pretty`.
  - `fp_check(symbol="Inductor:L")` passes.
  - `descr`, `tags` and `attr smd` are as given, and the file has no `(model`.
- **Second project.** `io` assigns `<nick>:L_Bourns_SRN3012BTA` through `sch_set_fields`; this
  adds the row and verifies existence.
- **Refusals write nothing.** An existing name, a missing source and a check error each leave the
  table and the library byte-identical (hashes before and after).
- **Compensation.** Inject a failure after S2 and after S3's save (a test hook in
  `FootprintChangeSet`). Then:
  - the footprint is gone, and the row is gone when this call created it;
  - an empty library directory created by the call is gone, but a directory that existed before
    is kept;
  - the error is the injected one (a failure in the read-back would be `verification_failed`),
    never success.
- **Compensation failure.** Also make `DeleteLibraryItem` fail. The result is
  `document_inconsistent`, and the notice names the leftover lib id. History undo then removes
  it.
- **Turn end after footprint writes.** The turn-end autosave succeeds (no footprint `DocKind`),
  and the next turn start reports **no** external change.
- **Same-turn use.** `fp_create`, then `sch_set_fields` with the new lib id in the same turn: the
  existence check passes (cache invalidated).
- **Undo of the first `fp_create`.** Snapshot (no row, no library directory yet), then
  `fp_create`, then restore. The `.kicad_mod` and the empty directory are gone, the table is as
  before, and `fp_get` → `not_found` after the reopen. Repeat with a library directory that
  already exists from another project: only this project's footprint is removed. The same for
  `fp_delete` → restore → the footprint is back.
- **Cross-project.**
  - `main` creates A; `io` creates B and approves A.
  - Restoring `main` to before A removes A only if `io` did not change it since; otherwise A is
    skipped and reported.
  - B is never touched.
  - `main` sees `io`'s writes as a library-only change: libraries reload, no plan pause.
- **`replace` compensation.** Inject a failure in S3 of a `replace`. The pre-image is rebuilt
  with an equal hash, or the error is `document_inconsistent`.
- **`fp_approve`.** The happy path, and the refusal after a `replace` that changed the hash.
- **`fp_delete` in use** → `in_use`.
- **`fp_render`.** The PNG exists. A CrtYd-only export (`--layers F.CrtYd`) has extents of
  4.0 × 4.0 within 0.01 mm. The full export's `viewBox` also contains the texts (spike Q8), so it
  is not asserted. The SVG route sends the CSP and `nosniff` headers and rejects names that are
  not uuids.
- **Coexistence.** An `fp_get` while the schematic has an open commit; the footprint document is
  always closed after each tool (`get_open_documents`); the schematic still works afterwards.
- **Stock footprint check.** `fp_check` on a stock footprint with `expected` (the U202 pattern).
  The test records the measured values of the stock DRC0010J footprint. The real datasheet
  comparison is a manual acceptance step.
- **CI.** The tests run in the sharded kicad job. The first-footprint activation costs about 3 s
  and about 1.7 GB per api-server, so the footprint tests share one module-scoped server
  fixture where isolation allows.

### 16.3 Manual acceptance (owner)

1. In the OpenDriveHub repo, a Schematic session creates L201 from the datasheet. The owner
   approves it in chat and assigns it.
2. KiCad's GUI opens the footprint, and the board (later) places it.
3. The J402 and U202 checks are run and their findings explained.

## 17. Phases (suggestion for the plan)

| Phase | Content | Depends on |
|---|---|---|
| P1 Read path | `FootprintDocument`, handler activation and `footprints_active`, `FootprintGeometry`, `fp_get`, `fp_list_local`, local-library resolution (§5, read side), `FP_READ` profiles, `footprint_libraries_ready()` | — |
| P2 Check + render | `checks.py` with all §8 rules, `fp_check`; the `fp_render` pipeline (kicad-cli, wrapper, CairoSVG), SVG serving with CSP | P1 |
| P3 History coverage | `tracked_files` (derived library dir), manifest v2, content-addressed library blobs, project-scoped library restore, library-only external-change handling, `library_changed` result field + hash re-recording, `invalidate_footprints` | — (parallel to P1/P2) |
| P4 Writes | `FootprintChangeSet` and engine, S1–S3 with compensation, `fp_create` (incl. `replace`), `fp_delete`, `fp_approve` with the `footprint_reviews` table, budget costs, change-log kinds, `sch_set_fields` / `sch_place_symbol` local check and row stage | P1, P2, P3 |
| P5 Skills + acceptance | `kicad-footprints` skill, `kicad-schematic-editing` / `part-notes` / `copperchat-tools` updates, research prompt (Land pattern), L201 golden end-to-end, docs (README tool list, CHANGELOG) | P4 |

Each phase is one PR with its own review loop. P1–P3 are useful on their own (reading and
checking stock footprints, J402/U202). P4 is the risky part and must not merge without P3 merged
first, because undo has to cover writes from the first day.
