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
| C4 | The first footprint access (`OpenDocument(DOCTYPE_FOOTPRINT)`, or `AddLibraryTableEntry(create_library)`, which registers the PCB kiface library handler) loads **every** footprint library in the tables: about +1.7–1.8 GB RSS and about 3 s. Up to 4 GB was observed under heavy reads. Once loaded, `GetItemsFromLibrary` is fast: 401 footprints in 0.2 s. | spike Q5, patch notes |
| C5 | Library table rows can be written only through patch 0001: `Add/Update/DeleteLibraryTableEntry` with `scope = LTS_PROJECT`. Pass `${KIPRJMOD}`-relative URIs, never substituted ones. `create_library = true` creates the empty `.pretty` directory, including parent directories, and is rolled back on failure. Status codes: `LCS_OK`, `LCS_ALREADY_EXISTS`, `LCS_NOT_FOUND`, `LCS_READ_ONLY`, `LCS_ERROR`. A row that shadows a global nickname cannot be created with `create_library` (`LCS_ERROR`). | patch 0001, `test_kicad_api_patches.py` |
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
- **Directory.** `<root>/footprints/<nick>.pretty`.
- **Nickname** (author call). The repo root directory name, cut down to `[A-Za-z0-9_-]` (other
  characters become `_`, at most 32 characters). Example: `OpenDriveHub`. If that name is already
  a nickname in the **global** table, `_local` is appended (C5: shadowing rows cannot be created).
  The resolved nickname is stored nowhere else: the project table row is the single source of
  truth.
- **URI.** `${KIPRJMOD}/` followed by the path from the project directory to the library,
  POSIX-style. Examples: `${KIPRJMOD}/../../footprints/OpenDriveHub.pretty` for
  `hardware/main/main.kicad_pro`; `${KIPRJMOD}/footprints/x.pretty` for a project at the root.
- **Row.**
  `AddLibraryTableEntry(type=LT_FOOTPRINT, scope=LTS_PROJECT, nickname, uri, plugin="KiCad",
  description="CopperChat local footprints", create_library=true)`.
  - `LCS_ALREADY_EXISTS` is fine when the existing row has the same URI.
  - A row with the same nickname but a different URI is an error (`library_conflict`). The
    footprint tool then refuses, and the message names both URIs.
  - If another project-scope row already points to the same `.pretty` directory (same resolved
    path), CopperChat reuses that row and its nickname (the user may have added it in the GUI).
- **When.** The row is created lazily, by the first `fp_create` in a project, as the first stage
  of that call (§9.3). The other projects of the repo get their row the first time a footprint
  tool writes in them, or when `sch_set_fields` assigns a local footprint there.
- **Local libraries** = project-scope rows whose resolved path lies inside the repo root.
  `fp_list_local`, `fp_delete` and the history coverage (§10) work only on these. Rows outside
  the root, global rows and stock libraries are read-only for CopperChat.

`LibraryService` (`library/service.py`) gets two methods: `local_library()`, which returns
`(nickname, uri, path) | None` from `GetLibraryTable(LT_FOOTPRINT, LTS_PROJECT)`, and
`ensure_local_library()`, which is used inside `apply` only.

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
- **No unloading.** The memory stays until the server restarts. A project switch restarts the
  server anyway (C14).
- **Logging.** On activation CopperChat logs RSS before and after. The first tool result in the
  turn gets a `notes` entry `{kind: "footprint_libraries_loaded", seconds}`, so that a slow call
  is explained.

### 6.2 `FootprintDocument`

`kicad_session/footprint_doc.py` (author call) holds the raw-protobuf helper, which grows out of
the test helper `FootprintDoc`.

- **Entry point.** `async with session.footprint_document(lib_id) as fd:`
- **Locks.**
  - It takes a new `KicadSession._fp_lock`: one footprint document per server (C2).
  - It runs each KiCad call through `session.call` as today.
  - For writing tools it also takes `_apply_lock`, through the adapter's edit lock.
  - Read-only tools that open a document (the `fp_get` fallback) hold only `_fp_lock`.
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
  "bbox": [xmin, ymin, xmax, ymax],
  "geometry_hash": "a1b2c3d4…"   // sha256 over the canonical pads + shapes (§9.4)
}
```

The default `detail` is `"summary"` (no `shapes` list). This keeps results small for footprints
with many graphics (author call).

### 7.2 `fp_get` (read-only, all session types)

- **Input.** `{lib_id: string, detail?: "summary" | "full"}`.
- **Output.** `FootprintGeometry`, plus:
  - `source` and `review` (`needs_user_review` / `approved`) for local footprints;
  - `notes`, e.g. `footprint_libraries_loaded`.
- **Errors.** `not_found` (with `details.closest` from the existing `_closest_footprints`),
  `invalid_argument` and `library_error`.
- **Path.** Batched `GetItemsFromLibrary` with an LRU cache by lib id. The cache is invalidated
  per library on every write by a CopperChat tool and on `on_turn_start` external changes.

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
  2. Run `kicad-cli fp export svg <dir.pretty> --fp <name> --layers … -o <tmpdir>`. The call
     uses an argv list (no shell), a timeout of 60 s, and an output directory that CopperChat
     created beforehand. CopperChat **checks that the SVG file exists and is not empty** (C10),
     because the exit code is no proof.
  3. CopperChat writes its own wrapper SVG. It holds:
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
     2000 px.
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
    "silkscreen"?: "auto" | {"segments": [[[x1, y1], [x2, y2]], …]},  // auto: body outline clipped 0.2 mm clear of pads
    "pin1_marker"?: "auto" | "none",                      // auto: silk + fab marker at pad 1
    "fields"?: {"Datasheet": "...", "...": "..."},        // ≤ 16 extra fields; Reference/Value are set by the tool
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
  - Validate the schema and the name. The name must be free in the local library unless
    `replace` is set; this is checked through `GetLibraryItems` (C1 overwrite).
  - Validate `source`:
    - a repo path must resolve inside the repo (`resolve_inside`) and exist;
    - a URL must be http(s);
    - `location` must be non-empty.
  - Validate the model path: it must be inside the repo or start with a KiCad 3D-model variable.
    It is a link only, so the file need not exist; if it is missing, a note says so.
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
  3. In an **earlier turn** of the same conversation, CopperChat produced an `fp_render` **and**
     an `fp_check` result for that hash. The server records these in a small per-conversation
     table `footprint_reviews(conversation_id, lib_id, geometry_hash, run_id, rendered_at,
     checked_at)`. "Earlier turn" means that a user message came between the render and the
     approval. That is the strongest server-side evidence available that the user saw it.
  4. Error-free `fp_check` on the current read-back.
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

- If the footprint's nickname is the **local** library:
  1. Ensure the project has the row (§5). If the row is missing but the library exists in the
     repo, `prepare` adds an op "add table row" to the change set. This costs 0 logical changes;
     the result reports it as `side_effects: ["fp_lib_table_row_added"]`.
  2. Activate the handler (§6.1).
  3. Check existence. An unknown name → `not_found` with `closest` from the local library.
  4. If the footprint has `needs_user_review = yes`, add the note `footprint_needs_review`. The
     assignment is allowed: the user may approve later.
- Otherwise, if `footprint_libraries_ready()` is true, the existing behaviour applies.
- Otherwise the existing `footprint_unverified` note stays. Its text changes from "this project
  has no board" to "footprint libraries are not loaded (no board, no footprint tool used)".

Writing the table row is the only KiCad write that `sch_set_fields` gains. It runs as the first
stage of the schematic change set, and its compensation is `DeleteLibraryTableEntry` (§9.3).

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

**`DocKind`** gains `"fplib"`. `save_after_edit` skips it, because footprint stages save inside
`apply`. The turn still reports it as changed, so history hashing (§10) and the UI change list
see it.

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
| S2 `copy_seed` | `SaveCopyOfDocument(seed → lib:name)`: a new `.kicad_mod`. With `replace`, the existing file is backed up in memory first (§9.5). | `DeleteLibraryItem` (patch 0003). With `replace`, the old footprint is rebuilt from the pre-image (§9.5). |
| S3 `rebuild` | Open the copy, then one commit: delete all children, create pads, graphics and fields, update footprint properties (patch 0002), remove the seed model or set the link; then `SaveDocument`. | A failure before save → `drop` the commit, then compensate S2. A failure after save → compensate S2. |

**If compensation itself fails**, CopperChat raises `document_inconsistent`, as the schematic
engine does. The turn ends. The turn snapshot (§10) restores the repo state, and the error message
names the leftover item. In that one case the anti-goal is met through history, not by the tool
itself.

**Cancellation.** The compensation runs shielded, like `KicadSession.commit` cleanup.

`fp_delete` and `fp_approve` have a single stage. `fp_approve` = one commit + save; a failure
before save is a `drop`.

### 9.4 Geometry hash

`geometry_hash` is a sha256 over a canonical JSON of the pads and of all shapes on Cu, Mask,
Paste, SilkS, Fab and CrtYd. Coordinates are rounded to 1 nm, and the list is sorted. Fields and
description are left out, so editing metadata does not invalidate an approval, but every
geometry change does. `fp_render`, `fp_check`, `fp_create` and `fp_approve` all report it.

### 9.5 `replace`

`fp_create(replace=true)` is allowed only for an existing **local** footprint. It counts as
deletion plus creation in one call:

- `prepare` reads the full pre-image: the footprint definition and its fields.
- S2 uses the **seed** again, so the result is built from scratch.
- Compensation on failure rebuilds the pre-image through the same S3 code path. The pre-image is
  API data, not file bytes.
- The result has `needs_user_review = yes`, and any `User review` field is dropped.

## 10. Undo, history and snapshots

C12 is extended in `history/store.py` (author call for the mechanism):

- **`document_files(project)` → `tracked_files(project, repo_root)`.** The tracked set is:
  - the existing project files;
  - the project's `fp-lib-table` (it may be absent);
  - every `*.kicad_mod` in each **local** library directory (§5) referenced by the project
    table.

  This covers the repo-root library above the project directory. Files outside the repo root are
  never tracked.
- **Manifest v2.** `relPath` becomes relative to the **repo root** (it was relative to the
  project directory before), and a new `trackedDirs` list names the local `.pretty` directories.
  Old manifests (v1, no version key) are still read with project-relative paths.
- **Restore.**
  - After copying the manifest files back, restore also **deletes `*.kicad_mod` files in a
    `trackedDirs` directory that are missing from the manifest**. These are footprints created
    after the snapshot; deleting them is what makes `fp_create` undoable.
  - If the snapshot had **no** `fp-lib-table`, restore deletes the table that exists now. The
    same applies to a library directory that did not exist then (a non-recursive `rmdir` of an
    empty `.pretty` directory after its files were removed).
  - These are restore-time file operations, like today's byte copy-back of `.kicad_sch` files.
    They are snapshot bytes written by KiCad, not CopperChat-authored content.
  - Restore already closes and reopens the session (`restore.py`), which reloads the tables and
    libraries. The `fp_get` cache is cleared.
- **External-change check.** `on_turn_start` hashes the tracked set, so a footprint edited in the
  KiCad GUI is noticed. The reaction is the existing reload-after-external-change.
- **Snapshot size.** Footprint files are small, a few KB each, so the full local library is
  copied per snapshot. This is fine for libraries with hundreds of footprints. A library above
  2,000 files logs a warning; there is no cap in v1.
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
- **The SVG link.** The SVG is served from `/api/projects/<id>/renders/<name>.svg` (the existing
  allowed prefix in `web/security.py`), with these headers:
  - `Content-Type: image/svg+xml`;
  - `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox`;
  - `X-Content-Type-Options: nosniff`.

  The agent may link it in markdown so the user can zoom. Markdown image rendering of SVG stays
  an `<img>` (no script execution). Field values in KiCad's SVG are escaped text, but the CSP is
  defence in depth.
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

- **Plans.** A plan step may contain footprint tools. `plan_propose` step kinds are free text
  today, so nothing changes there.
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

- The nickname is the repo directory name, with a `_local` suffix on a global collision; the row
  is created lazily by the first write (§5).
- A `footprints_active` flag, activation by opening a pinned stock footprint, and no unloading
  (§6.1).
- A single pinned seed `Inductor_SMD:L_Bourns-SRN4018` for SMD and THT (§6.3).
- The `FootprintGeometry` model, with `detail = summary` as the default (§7.1).
- `fp_render`: CopperChat's own wrapper SVG with grid, axes and pad labels, rasterised to PNG
  with CairoSVG (a new dependency); the SVG is served with a sandbox CSP (§7.5, §11).
- A separate `fp_approve` tool with a geometry hash, a "rendered and checked in an earlier turn"
  guard and a `User review` field (§7.7).
- `fp_delete`: refused if the footprint is used in the open project; only a warning when it may
  be used in other projects of the repo (§7.8).
- The `fp_check` rules, thresholds and severities (§8); no new geometry library.
- `FootprintChangeSet`, its stages and compensation; `rmdir` of an empty library directory
  created by the failing call (§9).
- A geometry hash that ignores metadata (§9.4).
- `replace` rebuilds from the seed, and compensation uses the pre-image (§9.5).
- Manifest v2, repo-root-relative, with deletion of untracked `.kicad_mod` files on restore (§10).
- Budget costs: `fp_create` 5, `fp_approve` / `fp_delete` 1, the row 0 (§12).
- `fp_check` / `fp_list_local` in all profiles; a separate `kicad-footprints` skill; an optional
  `## Land pattern` section in part notes (§13).

## 15. Open questions for the owner

1. **How the approval is recorded.** With an agent tool (`fp_approve`, guarded by "earlier turn +
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

## 16. Testing

### 16.1 Unit tests (`backend/tests/unit/footprints/`, no KiCad)

- `checks.py`: one test per rule and severity on synthetic `FootprintGeometry`. This includes:
  - the J402 case (9 pins, 11 pads) → a `pad_without_pin` warning for 10/11, or info with
    `allow_unmatched_pads`;
  - a DRC0010J-like 10 + EP case with `expected` mismatches → `expected_mismatch`.
- `model.py`: protobuf → geometry, using recorded protobuf fixtures; the canonical
  `geometry_hash` is stable and changes on geometry changes only.
- Schema and `prepare` validation: name regex, source rules (path escape, missing file, empty
  location), model path rules, `replace` on non-local, the overwrite refusal.
- Nickname and URI derivation (§5) for nested, root and non-git projects, and for a global
  collision.
- `fp_approve` guards: same turn, wrong hash, no render, no check, check errors.
- History: `tracked_files`, manifest v2 read/write, v1 compatibility, restore deleting untracked
  `.kicad_mod` files and the table (`tmp_path`, no KiCad).
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
  `FootprintChangeSet`). Then the footprint is gone, and the row is gone when this call created
  it. The error is `verification_failed`, not success.
- **Undo.** Snapshot, then `fp_create`, then restore. The `.kicad_mod` is gone, the table is
  back, and `fp_get` → `not_found` after the reopen. The same for `fp_delete` → restore → the
  footprint is back.
- **`fp_approve`.** The happy path, and the refusal after a `replace` that changed the hash.
- **`fp_delete` in use** → `in_use`.
- **`fp_render`.** The PNG exists; the SVG `viewBox` extents match the CrtYd 4.0 × 4.0 within
  0.01 mm.
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
| P3 History coverage | `tracked_files`, manifest v2, restore deletes, external-change hashing, `DocKind "fplib"` | — (parallel to P1/P2) |
| P4 Writes | `FootprintChangeSet` and engine, S1–S3 with compensation, `fp_create` (incl. `replace`), `fp_delete`, `fp_approve` with the `footprint_reviews` table, budget costs, change-log kinds, `sch_set_fields` / `sch_place_symbol` local check and row stage | P1, P2, P3 |
| P5 Skills + acceptance | `kicad-footprints` skill, `kicad-schematic-editing` / `part-notes` / `copperchat-tools` updates, research prompt (Land pattern), L201 golden end-to-end, docs (README tool list, CHANGELOG) | P4 |

Each phase is one PR with its own review loop. P1–P3 are useful on their own (reading and
checking stock footprints, J402/U202). P4 is the risky part and must not merge without P3 merged
first, because undo has to cover writes from the first day.
