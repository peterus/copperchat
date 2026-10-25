# Changelog

All notable changes to CopperChat are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Container images:
`ghcr.io/peterus/copperchat:<version>`.

## [Unreleased]

## [0.5.0] - 2026-10-25

**Schematic addressability and ergonomics** (PM topic #15): the agent can find, address and
turn more schematic items without deleting and re-creating them. Status per item:

### Added

- **#1 sheet pins addressable: DONE.** `sch_move` / `sch_delete` accept a sheet pin by UUID
  or `'<sheet>.<pin>'`. A move snaps the pin onto the nearest edge of its sheet symbol; a
  side change sets the matching spin style. Delete updates the sheet symbol without the pin
  (KiCad's API cannot delete a sheet pin by UUID) and removes the child's hierarchical label
  only if nothing else uses it.
- **#2 rotate / mirror labels and sheet symbols: DONE.** Labels turn about their anchor
  (absolute text direction); sheet symbols turn about their centre with their pins and
  autoplaced fields. No KiCad patch needed.
- **#3 finding items: DONE.** New read tool `sch_list_items` (sheet, types, bbox, net, text
  filter, paging). `sch_get_pin` gains `stub_end` / `stub_wires`, and `connected` lists
  sheet pins.
- **#4 `expect_net`: DONE.** `sch_move(..., expect_net=...)` declares the net a move must
  join; anything else is refused and rolled back with "would not connect to …" / "would
  also connect to …".
- **#12 ERC ergonomics: DONE.** `run_erc(sheet?, limit, offset)` returns violations with
  stable ids, totals and counts per type and sheet. `plan_step_done` takes
  `erc_rules: [{match: {type?, sheet?}, reason}]`; justifications are stored per step.
- **#13 plan budget: DONE.** The budget counts logical operations (`sch_delete` books all
  items on one net as one change); every mutating tool description states its cost.
  `plan_revise` works while a step runs: the step is paused, keeps its counters and resumes
  in the new revision. Rejecting such a revision restores the previous one, paused.
- **#20 rotate / mirror carries owned stubs: DONE.** Owned stub chains (wires, power
  symbols, labels, no-connects) turn along with their part, as on translation. A collision
  of a carried item with a foreign item refuses the whole call before writing.

### Fixed

- **#19: DONE.** `geometry._field_box` uses the measured text extents (one constant set)
  instead of 0.6 em per character.

### Changed behaviour

- ERC violation ids now include the sheet (type + sheet + item UUIDs), so instances of a
  reused sheet get distinct ids.
- Moving or rotating a sheet carries its pins' own stubs (before: dragged) and refuses
  landing over wires or labels that stay.
- A sheet-pin side change whose wire cannot follow is refused.
- Placing a symbol pin onto a sheet pin is refused.
- A revised plan is re-posted at the bottom of the chat for approval; earlier plan cards
  collapse to "Superseded by revision N".
- A step interrupted by a turn end resumes with its persisted counters.
- **Migrations 0010 and 0011** (`plan_steps` rebuilt, per-step ERC justifications). Back up
  `data/` before upgrading.

No KiCad patches needed.

### Known limitations / deferred

- Orphan ERC justification rows of deleted plans are not cleaned up.
- Item-less duplicate ERC violations on one sheet share an id.
- Concurrent `run_erc` pages each run `kicad-cli`.
- Carried items are not checked against other parts moved in the same call (PM #26).
- `expect_net` on a child file with several instances is refused (PM #27).
- A turned label's text is not checked against wires re-laid in the same move.
- The hierarchy is re-read per edit helper call (PM #24).

## [0.4.1] - 2026-10-24

**Schematic editing correctness and data loss**: fixes for the `sch_*` problems found while
editing real designs. Status per item:

### Fixed

- **#6 data loss: FIXED.** The compensation of a failed edit re-creates wires that KiCad
  had merged away, so a failed `sch_move` no longer ends in an inconsistent document. The
  design is saved after every successful edit (typically 15–130 ms, growing with schematic
  size: ~0.9 s at 20 MiB); a failure reopens the last save, which is the state before the
  failed edit, so earlier edits of the turn are kept (unless saving itself fails). The user sees a plain-language notice, and the chat no longer claims "the agent
  could not start".
- **Left sheet pins: FIXED.** KiCad's API treats an unset spin style as LEFT; CopperChat
  now sends it explicitly. No KiCad patch needed.
- **#7/#8: FIXED.** Translating a symbol (`sch_move`) carries its owned stub chains and
  the power symbols, labels and no-connects on them. Rotate and mirror still only drag the
  wires.
- **#9: FIXED.** One 2.54 mm sheet-edge clearance rule for `sch_connect`, `sch_add_wire`,
  `sch_reroute` and move; legacy violations in existing designs only warn.
- **#10: FIXED.** The `sch_set_fields` description now matches its schema
  (`fields={...}`); a test checks all tool descriptions.
- **#11: FIXED.** Stub routes prefer candidates that keep the room of neighbouring pins
  free (no net merge happened before either).
- **#14: FIXED.** Text-width constants are measured from KiCad; placing a symbol reports a
  field overlapping its own body or pins.
- **#5:** the label column is FIXED (2.54 mm pitch, up to 4 columns); the parallel
  collision is NOT REPRODUCIBLE (editing holds a project-wide lock).

### Known follow-ups

- `geometry._field_box` still assumes 0.6 em text width.
- KiCad API quirks noted for a possible upstream issue (nothing submitted): `update_items`
  ignores unknown ids; an unset spin style means LEFT.

## [0.4.0] - 2026-10-22

**Repository context**: the agent can read (never write) its project's repository and
gets a short project brief at session start.

### Added

- **Repository context.** The agent can read the project's repository (the git top level
  inside the projects folder, else the project folder) in every session type, through
  three read-only tools: `repo_list` (files and folders, kinds text/binary/kicad/large),
  `repo_grep` (literal or regex search in a killable worker, stops at 100 results, 8 MiB or
  5 s) and `repo_read` (1000 lines / 64 KiB per call, files up to 1 MiB). A block list hides
  `.git`, `.env*`, keys, credentials, CopperChat state and KiCad backups; `.gitignore`d
  files, submodules and symlinks out of the root are refused; KiCad files and binaries are
  listed but not readable. Paths are checked in `can_use_tool`, the PreToolUse hook and the
  tool itself; built-in file tools stay denied.
- **Project brief.** At the first turn of a session the agent gets the repository name, the
  project's location in it, a README excerpt (fenced as project data, never instructions)
  and a folder tree, at most 6 KiB. See
  [docs/user/repo-context.md](docs/user/repo-context.md).

## [0.3.0] - 2026-10-20

Sub-project 3: **sessions and design docs**. Several chat sessions per project, typed by
what the agent may do, working from shared design docs under `docs/design/`.

**Upgrade: back up `data/` before you start 0.3.0.** Migration `0008` moves the database
to a schema v0.2 cannot read. Each project's chat becomes "Session 1" (Free) with its full
history, and its agent session is resumed. See
[docs/user/sessions.md](docs/user/sessions.md#upgrading-from-v02).

### Added

- **Sessions.** Several conversations per project, at `/p/<project>/s/<session>`: a session
  header (type, inline rename, status, Stop for the session that runs), a session list
  (activity, unread and running dots, cost, archive), a new-session dialog. One run per
  project; the messages of other sessions queue in order, and Stop is scoped to one
  session. Push notifications open the session.
- **Session types.** **Requirements** (doc tools, part research, library and read-only
  design tools, no schematic writes), **Schematic** (all schematic tools; in the docs only
  decisions, `Open points` and `Key parts`) and **Free** (everything). Each type has its
  own tool profile, enforced in the tool list, `can_use_tool` and a PreToolUse hook, plus
  its own system prompt and skills (`session-requirements`, `session-schematic`,
  `design-docs`). A skill's `sessions:` front-matter key limits it to some types, shown as
  chips on the Skills page and editable on user skills.
- **Design docs** `requirements.md`, `architecture.md`, `parts.md`, `decisions.md` (and
  extra docs, two levels deep) from templates, written by the agent through five doc tools
  (`doc_list`, `doc_read`, `doc_write`, `doc_patch_section`, `doc_append`) with a
  per-type write policy, a body-hash version on every write (conflicts never clobber), a
  server-owned front matter and full revisions.
- **Docs panel.** Index with status, open points and last author; rendered view with links
  to part notes; editor with conflict flow; approve/unapprove; "Let the agent rewrite";
  history with line diffs and revert; live updates. Chat cards "📄 doc updated — +n / −m
  (sections)" open the diff.
- **Approval and the soft gate.** Only the user approves, and any later change makes the doc
  a draft again (with a notice). Creating a Schematic session from unapproved requirements
  shows a warning; the agent mentions it once and goes on.
- **Close session (handover).** The agent brings the docs up to date in one last turn; the
  server records its summary as a `Session N closed` decision. Closed sessions are
  read-only until reopened. Stuck closes are recovered at startup.
- **Context injection.** A new session starts from a doc index with summaries of
  `parts.md` and the latest decisions (and, for Schematic, the fenced text of
  `requirements.md` if it is at most 8 KiB), not from older chats; later turns get notices of doc
  changes.
- **Live tests** (opt-in, real model): a Requirements guard test (out-of-profile tool,
  `/etc/passwd`, an injected doc asking for exfiltration: all refused), the sessions eval
  (requirements session → approval → schematic session from the docs: golden netlist,
  ERC 0/0) and a v0.2.0 → 0.3.0 upgrade check (`scripts/live-upgrade-check.sh`). Results
  in `backend/tests/live/results/`.

### Changed

- Database migrations `0008_conversations` (conversations; messages, runs and plans per
  session) and `0009_design_docs` (doc revisions and approvals).
- `research_part` queries are capped at 2,000 characters, and the researcher must not put
  design details into URLs or searches.
- A plan belongs to the session that proposed it; other sessions are told which session
  owns it. A session that owns an open plan cannot be closed.
- History and Changes entries show their session.
- Old browser tabs show a reload banner after an upgrade.

## [0.2.0] - 2026-10-19

Sub-project 2: **schematic editing**. The agent changes schematics through KiCad's IPC API
only; boards stay read-only.

### Added

- **Editing tools.** `sch_place_symbol`, `sch_set_fields`, `sch_find_space`, `sch_annotate`,
  `sch_connect` (computed orthogonal routes with T-joins, label fallback), `sch_add_label`,
  `sch_add_power`, `sch_no_connect`, `sch_add_wire`, `sch_rename_net`, `sch_reroute`,
  `sch_move` (drags wires), `sch_delete` (with attached stubs, labels and power symbols),
  `sch_duplicate`, `sch_add_sheet`, `sch_add_sheet_pin`, `sch_get_pin`, `sch_changes`. Every
  mutating call is one verified change: prepared first (refusals change nothing and take no
  snapshot), written in KiCad commits, read back against KiCad's netlist and undone on a
  mismatch.
- **Library tools** `lib_search_symbols`, `lib_get_symbol`, `lib_search_footprints` (symbol
  libraries loaded eagerly, footprint libraries on demand), `parts_get_notes`, and layered
  **part preferences** (default: passives 0805; deviations are flagged).
- **Plans.** Larger changes are proposed as a plan card (Approve / Reject / Resume / Roll back /
  Abort); steps run in order, each ends with an ERC delta (new errors fixed or justified) and a
  checkpoint. Change budget per turn (default 20 logical changes, at most 3 deletions of
  existing parts) and plan policy in **Settings › Editing**.
- **Visual control.** Change log per turn and step, `kicad-cli sch diff` before/after layer,
  highlights of added/modified/removed items in the viewer (KiCanvas fork), a **Changes** panel,
  plan steps nested in the history.
- **Skill `kicad-schematic-editing`** (tool order, pin addressing, rails and labels, plans, ERC
  policy, self-check, missing parts); placement recipes in `kicad-schematic-style`.
- **Tests.** Golden scenario tests build three reference circuits (LED, LDO, MCU with an I2C
  sensor on a sub-sheet) from an empty project through the tools against real KiCad; undo and
  rollback tests; edited KiCad-saved fixtures load in KiCanvas; an opt-in live eval
  (`COPPERCHAT_LIVE_EVAL=1 pytest -m live_sdk tests/live/test_editing_eval.py`, results in
  `backend/tests/live/results/`).

### Changed

- The agent's system prompt allows schematic edits (boards stay read-only).
- Database migrations `0006_editing` (editing settings, change log, ERC results, plans) and
  `0007_changes_created_index`.

## [0.1.1] - 2026-10-17

Smaller container images and a faster, branch-safe kicad-base CI build. No feature changes.

### Changed

- **Smaller images.** The `kicad-base` image shrinks from 2.97 GB to 1.95 GB and the app image
  from 3.76 GB to 2.54 GB (compressed `kicad-base`: 663 MB to 425 MB). Removed: debug symbols,
  docs and locales, Mesa/LLVM drivers (KiCad renders on the CPU headless), OpenCASCADE sample
  data and all KiCad demos except the test fixtures; `uv` is no longer shipped in the app image.
  STEP/GLB export, 3D render, DRC/ERC, exports, the api-server, kipy and `pdftoppm` are unchanged.
- **Recipe-pinned `kicad-base` tags.** Branches publish `<commit>-r<recipe>`; only `main`
  publishes `<commit>`, and it always builds what it publishes.
- **Shared registry ccache.** `main` keeps a `:ccache` image that branches seed from, so a
  warm `kicad-base` build takes minutes instead of about 2 hours.

### Fixed

- The `ldd` closure check covers `*.kiface` files, the kipy venv, Python extension modules and
  git, and fails if a path-exclude removes a linked library.
- Release image pushes pass the `kicad-base` build argument.
- The end-to-end smoke test no longer collides between concurrent runs.

## [0.1.0] - 2026-10-17

First public release: the **foundation** — chat with a read-only AI agent about your KiCad
projects, in the browser on desktop, iPad and iPhone.

### Added

- **Chat agent with read-only KiCad tools.** The agent inspects schematics and boards through
  KiCad 11's own API (`kicad-cli api-server` + kipy), runs ERC/DRC, renders annotated
  "agent views" and looks at them. It has no file, shell or web tools of its own. Message
  queue, Stop, resume after rate limits, usage display, image/PDF attachments.
- **Viewer.** Schematic and board viewer based on KiCanvas, patched for the KiCad 11 file
  format, touch gestures and host integration (sheet tree, layers, pinch zoom); tap a part to
  mention it in the chat (`@R12`).
- **History, undo and git.** Autosave after every turn, per-turn undo snapshots, detection of
  external changes, crash recovery; git status, diff summary and commit — only on request,
  never push or history rewriting.
- **Skills and part researcher.** Built-in, user and per-project skills; a separate, guarded
  part-researcher agent (`research_part`, public web only) that collects datasheets and
  pinout/application notes under `docs/parts/<MPN>/` in the project.
- **Uploads** of images and PDFs per project (the agent can read PDF pages).
- **Project picker** over the mounted projects folder (any depth); projects open in the
  KiCad GUI open read-only with a banner.
- **Installable app (PWA) and Web Push** when a run finishes, the agent needs you or a
  rate-limit window reopens.
- **Security.** App login always on (argon2, rate-limited), strict Content-Security-Policy,
  WebSocket origin check, push endpoints restricted to the browsers' push services.
- **Deployment.** Docker image `ghcr.io/peterus/copperchat` (KiCad 11 built from source,
  runs as uid 1000), `docker-compose.yml` with an optional Cloudflare Tunnel profile
  (`docker compose --profile tunnel up -d`), `/api/health` with app and KiCad version.

### Known limitations

- **No editing yet:** the agent cannot change schematics or boards (planned for the next
  sub-project, with plan-before-edit).
- One user per instance.
- No light theme for the board viewer.
- Web Push delivery is not tested in CI, and the app has not been tested on a real iPhone
  yet.
- The optional `cloudflared` service uses the unpinned `cloudflare/cloudflared:latest` image.
- The KiCanvas patches live in this repository (`frontend/vendor-patches/kicanvas/`), not in
  an upstream fork; the main JavaScript chunk is large (~590 kB).

[Unreleased]: https://github.com/peterus/copperchat/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/peterus/copperchat/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/peterus/copperchat/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/peterus/copperchat/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/peterus/copperchat/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/peterus/copperchat/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/peterus/copperchat/releases/tag/v0.1.0
