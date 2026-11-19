---
name: kicad-footprints
description: Read, check, create, render, approve and delete footprints; library order; no invented dimensions. Use whenever a part needs a footprint, a footprint must be checked against its datasheet, or the user asks to approve or remove one.
sessions: schematic, free
---

# Footprints

Footprints are read, checked and drawn with `fp_get`, `fp_list_local`, `fp_check` and
`fp_render` (read-only, every session type). `fp_create`, `fp_approve` and `fp_delete`
write; they exist only in Schematic and Free sessions. Coordinates are mm in footprint
coordinates: origin = anchor, x right, **y down**; angles in degrees. A `lib_id` is
`<nickname>:<name>`. CopperChat writes only to the repository's **local library**
`footprints/<nick>.pretty` at the repo root (`<nick>` = the repo folder name, `_local`
appended if a stock library has that name); every other library is read-only. The first
`fp_create` adds the project's library row (`library_created: true`).

**Shared library:** all projects of the repo share it, but a project sees it only once its
own row exists. A footprint another project created (`fp_list_local` / `fp_get` note
`no_local_library` with an on-disk count, or `already_exists` "on disk") is **reused, never
recreated or replaced**: assign it with `sch_set_fields` (`footprint: "<nick>:<name>"`),
which adds this project's row, then check it with `fp_get` / `fp_check`.

## Library rule

Take a footprint from the first source that has one, in this order:

1. KiCad stock (`lib_search_footprints`);
2. manufacturer-provided (already in a project library);
3. third-party (already in a project library);
4. handmade with `fp_create`, last.

Whatever the origin, use a footprint only after `fp_check(lib_id, symbol=…,
expected=…)` against the datasheet's land pattern passed. A stock footprint that
differs (`expected_mismatch`, `expected_missing_pad`) is never used silently: tell the
user what differs and by how much, and let them choose.

## No invented dimensions

- Every number in `fp_create` comes from the cited source: the datasheet's
  **recommended land pattern**, preferred over the package drawing. The part notes'
  `## Land pattern` section (written by `research_part`) carries it with page numbers.
- What the source does not give uses the documented defaults, and the approval message
  names them: courtyard 0.25 mm around pads + body (rounded outward to 0.01 mm),
  silkscreen = the body outline kept 0.2 mm clear of copper, pin-1 mark on silk and a
  chamfered F.Fab corner, roundrect ratio 0.25, pad layers by type.
- No land pattern in the datasheet → ask the user. Never derive pads from the package
  size.

## Procedure

1. `parts_get_notes(mpn)` (or `research_part` when there are no notes): the land
   pattern, the datasheet path and page.
2. Look for a stock match first (library rule); check it with `fp_check`.
3. Otherwise `fp_create` with: `name` (KiCad style, `L_Bourns_SRN3012BTA`),
   `description`, `mounting` (`smd` / `tht`), `pads` (number, type, shape
   rect/roundrect/circle/oval, `at`, `size`, `drill` for THT), `body` (F.Fab outline,
   required), optional `courtyard`, `silkscreen`, `pin1_marker`, `keywords`, `fields`,
   `model` (a 3D model link only), the **mandatory** `source` (`document` = path relative
   to the **repository root**, e.g. `hardware/main/docs/parts/<MPN>/datasheet.pdf` when
   the project lives in `hardware/main` — part notes sit under `<project
   dir>/docs/parts/<MPN>/`; list files with `repo_list` — or an http(s) URL; `location` = page,
   figure or table) and `symbol` (`Device:L` or a placed reference) so the pins are
   checked. Extra `fields` must not be named Reference, Value, Footprint source,
   needs_user_review or User review (set by the tool) or `Description` (KiCad's
   built-in field: put the text into `description`). `Datasheet` is allowed.
4. `fp_render(lib_id)` and look at the image.
5. Present, in one message: the render (and the `svg_url` as a markdown link for
   zooming), every `fp_check` warning, the source document + location, the key
   dimensions **measured from the read-back** (`geometry` of the `fp_create` result or
   `fp_get`; not copied from your input), the defaults you used, and the question
   **"Approve this footprint?"**. Then **stop the turn**.
6. Only in a **later** turn, after the user's own message says ok: `fp_approve(lib_id,
   geometry_hash, user_quote)` with `user_quote` copied verbatim from that message
   (at least 2 words and 8 characters, or their whole message). Never approve on your
   own, in the turn that rendered it, or while a plan is running. If the user asks for
   changes: `fp_create` with `replace: true` (the review starts again).
7. Assign it with `sch_set_fields` (or place with it). Assigning before the approval is
   allowed; the result then carries `footprint_needs_review`.

## The tools as built

- `fp_get(lib_id, detail)`: pads (number, type, shape, at, rotation, size, drill,
  layers), per-layer `graphics` summary, texts, models, `bbox` (pads + shapes), mounting,
  fields, `geometry_hash`; for local footprints also `source` and `review`
  (`unreviewed` / `needs_user_review` / `approved`). `detail: "full"` adds the shapes.
- `fp_list_local(query, limit)`: `library` (nickname, uri, path_rel_repo; null with a
  `no_local_library` note before the first `fp_create`) and `footprints` (name, lib_id,
  description, pad_count, `needs_user_review` (= the review state), source, `used_by` = references in this project).
- `fp_check(lib_id, symbol, expected, allow_unmatched_pads)`: `passed` (no errors),
  `counts` (error / warning / info), `findings` (rule, severity, message, pads, at,
  measured, expected), `geometry_hash`. `expected` = `pads` [{number, at, size, drill}],
  `pitch`, `courtyard`, `body`, `tolerance_mm` (default 0.01). Errors block `fp_create`
  and `fp_approve`.
- `fp_render(lib_id, layers, grid_mm, labels)`: the PNG (shown to you and in the chat)
  and `image`, `svg`, `svg_url`, `scale_px_per_mm`, `extent_mm` (including the render
  margins, so not the footprint size), `origin_mm`, `grid_mm`, `geometry_hash`, `notes`.
  Measure sizes with `fp_get` or `fp_check`, not from the picture. A render adds about
  0.3–0.5 s (large footprints up to a few seconds).
- `fp_create`: checks the target first (`check_failed` on any error), builds it through
  KiCad and reads it back. Result: `created`, `file`, `library_created`, `check` (on
  the read-back; it counts as the `fp_check` for the approval), `geometry`,
  `needs_user_review: true`, `notes`. Counts 5 changes of the turn budget (at the default
  budget of 20, 4 new footprints per turn without a plan).
- `fp_approve`: the server refuses (`approval_not_allowed`, `details.guard` 1–7) unless
  the footprint is local and awaits review, the hash is the current one, this session
  ran `fp_render` and `fp_check` on that geometry **after** its latest
  `fp_create`/`replace`, the current turn was started by a message the user typed
  after both, `user_quote` is in it, no plan is running and the check has no error.
  Result: `approved`, `review`, `geometry_hash`, `user_review`. Counts 1 change.
- `fp_delete(lib_id)`: only when the user's current message asks for it. Local
  footprints only (any of them, also ones the user saved by hand; History undo restores). Refused with `in_use` (`details.refs`)
  while a symbol of the open schematic names it: reassign those first. Result: `deleted`,
  `file`, `notes`. `in_use_on_board` (this project's board places it) and
  `possibly_in_use_elsewhere` (another project of the repo names it, or the search was
  incomplete) are warnings: tell the user. History undo brings it back. Counts 1 change.

## Checking existing footprints

For a footprint the design already uses (or a stock candidate), call `fp_check` with
`symbol` (the placed reference, e.g. `U202`) and `expected` (the datasheet land
pattern). Report every `expected_mismatch` with measured and expected values.
`pad_without_pin` warnings for shell, shield or mounting pads (e.g. the two shell pads of
a microSD socket) are expected: explain them to the user and pass those pad numbers in
`allow_unmatched_pads`, which turns them into info. `pin_without_pad` is an error: the
footprint does not fit the symbol.

## Memory and time

The first footprint tool of a project without a board (and the first read of a local
footprint) loads KiCad's footprint libraries: a few seconds (about 6 s measured) and
about 3 GB of server memory, once per server. The result then carries the note
`footprint_libraries_loaded` with the `seconds`; mention it only if the user waits.

## Notes

- `footprint_libraries_loaded`: the libraries were loaded by this call (`seconds`).
- `footprint_attributes_unavailable`: a non-local footprint read from the library list:
  mounting shows `unspecified`, no Reference/Value texts or BOM/DNP flags.
- `footprint_needs_review` (from `sch_set_fields` / `sch_place_symbol`): the assigned
  local footprint is not approved yet; show it with `fp_render` and ask.
- `footprint_review_unknown`: the edit added the library row, so the state could not be
  read before; call `fp_get` afterwards and render and ask only if `review` is not
  `approved`.
- `footprint_unverified`: libraries not loaded, the name was not checked. Tell the user.
- `model_missing`: the 3D model file is not there; the link is kept.
- `no_local_library`, `library_conflict` (as a note of `fp_list_local`): no usable local
  library row yet / a row conflicts with it (writes will refuse).
- `footprint_unreadable`: KiCad lists footprints it cannot load (`lib_ids`).
- `strings_truncated`: long descriptions or sources were cut.
- `pending_cleaned`: an unfinished copy of an earlier crashed call was removed.
- `pending_leftover`: a crashed call left something it could not clean up; say what,
  check it with `fp_get` / `fp_check`, never delete a footprint the user already had.
- `possibly_in_use_elsewhere`, `in_use_on_board`: see `fp_delete` above.

## Errors

- `not_found`: wrong lib_id; use `details.closest` or `lib_search_footprints`.
- `invalid_argument`: fix the argument named in the error (name, field, source path).
- `library_error`: KiCad refused the library request; try another library or ask the
  user to check the library tables.
- `libraries_loading`: wait a few seconds and call again.
- `footprint_tools_disabled`: the server has footprint tools off; use
  `lib_search_footprints` and stock footprints only.
- `seed_missing`: KiCad's stock libraries are not installed; tell the user.
- `repo_root_unknown`: git could not answer; try again in a moment.
- `library_conflict`: a library-table row clashes with the local library; tell the user
  which row (from the message) to rename or remove.
- `not_local`: only the local library can be written; pick a stock or local footprint.
- `already_exists`: if the message says "on disk" (made by another project of the
  repo), reuse it as described under "Shared library"; otherwise choose another name,
  or `replace: true` after the user's OK.
- `check_failed`: fix every error in `details.findings` before calling again.
- `render_failed`: say the render failed and describe the footprint from `fp_get`.
- `verification_failed`: KiCad's read-back differs or the footprint changed meanwhile.
  Follow the error's suggestion: after `fp_create`, read the footprint again and retry
  once; after `fp_delete` of a footprint that changed, ask the user before deleting it;
  if `fp_delete` or `fp_approve` could not be confirmed, tell the user and do not retry
  in this turn.
- `document_inconsistent`: a failed write could not be undone completely; stop and
  tell the user what is left over (the message says it). Undoing the turn in History
  restores the earlier state; never delete a footprint the message calls the user's.
- `approval_not_allowed`: do not retry in this turn; show the render and the check again
  and ask the user to look and confirm in their next message (`details.guard` says why).
- `in_use`: assign another footprint to `details.refs` first, or keep it.
