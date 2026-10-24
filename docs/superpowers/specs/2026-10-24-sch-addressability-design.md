# sch_* tools: addressability and ergonomics (v0.5.0) — design

Status: draft for owner review (2026-10-24). PM topic #15 ("Paket B"). Follows
the patch release v0.4.1 (PM topic #14, spec
`2026-10-23-sch-fixes-design.md`), before sub-project 4 (board).

## 1. Purpose

The same practice session on the OpenDriveHub main PCB that produced v0.4.1
showed a second class of problems: not wrong behaviour, but **missing
capabilities**. The agent maneuvered itself into dead ends because it could not
address, find or rotate the things it had placed wrongly — sheet pins, labels,
wires — and so could not repair its own mistakes except by deleting and
recreating. On top, ERC justifications and the plan budget slowed it down for no
safety gain.

Goal: the agent can **find, address and repair** every schematic item it can
create, without detours, while the safety rails from sub-project 2 (connectivity
verification, hard budget stop, approval of plans) stay intact.

## 2. Scope

### Goals

| Item | Short |
|------|-------|
| #1 | Sheet pins addressable (UUID or `"<Sheet>.<Pin>"`): move, change side, delete |
| #2 | `sch_move` rotates / mirrors labels and sheet symbols, not only symbols |
| #3 | Find labels, wires, junctions, … with UUID and position (`sch_list_items`); `sch_get_pin` shows what hangs at the far end of a stub |
| #4 | `expect_net` on `sch_move`: a declared new connection passes verification |
| #12 | ERC: sheet filter, ids and pagination in `run_erc`, pattern justifications, persisted per plan |
| #13 | Plan budget counts logical operations; `plan_revise` from a running step; revised plan re-posted for approval |
| #19 | One set of text-extent constants for field boxes (layout and obstacles) |
| #20 | Rotate / mirror carries the symbol's own stub chains |

### Non-goals

- Board / PCB tools (sub-project 4).
- UI beyond what the tool changes require (the only UI change is §9.4).
- Submitting the two known KiCad upstream issues from v0.4.1 (PM topic #21).
- Any loosening of the connectivity verification or the budget stop beyond
  §7 (`expect_net`) and §9 (counting).
- A project-wide (cross-plan) ERC justification store (§8.4).

### Anti-goals

- A blanket `allow_merge` or any switch that turns verification off (owner
  rejected it explicitly).
- A large CopperChat workaround for something that is cleaner to fix in KiCad
  (owner guideline, PM topic #16).
- Hand-editing KiCad files, ever. All edits go through the KiCad IPC API.
- A tool that produces geometry another tool rejects (carried over from v0.4.1).
- Silent half-success: a call either does all of what it says or nothing.

## 3. Working method (binding)

As in v0.4.1 §3, per item: reproduce on a small test project against real KiCad
in the container → locate (CopperChat / KiCad IPC API / kicad-python) → test
first (fails on today's main, `backend/tests/kicad/edit/` style where possible,
unit tests in `tests/unit/` otherwise) → implement → record status for the final
report: *done* / *in KiCad* (patch + repro) / *open*.

KiCad fixes follow v0.4.1 §13: local fork commits carried as patch files in
`docker/kicad-base/patches/`, new pinned base image; **nothing is submitted
upstream without the owner seeing it first.**

## 4. Owner decisions (2026-10-23/24)

- **O1 (#1):** Sheet pins are repaired through the existing tools — `sch_move`
  and `sch_delete` accept sheet pins — not through a new tool. The duplicate
  pin-name ban stays.
- **O2 (#2):** Labels and sheet symbols rotate / mirror in `sch_move`.
- **O3 (#3):** A new tool `sch_list_items`, not a detail mode of `sch_get_net`.
  `sch_get_pin` additionally reports what sits at the far end of its own stub.
- **O4 (#4):** `expect_net` instead of `allow_merge`; verification stays active
  and checks against the declared intent.
- **O5 (#12):** As proposed in §8 (sheet filter, ids in `run_erc`, pagination,
  pattern justifications, persisted). The persistence scope was left to the
  author (§8.4, §17).
- **O6 (#13):** Count logical operations as in §9.1. `plan_revise` is allowed
  from a running step (pause, revise, continue). The hard stop stays.
- **O7 (#13, UI):** A revision that needs approval must not force the user to
  scroll up: the revised plan is posted again at the bottom of the chat with its
  own approve button; the old card collapses to "superseded by revision N".
- **O8 (#20):** Rotate / mirror carries the own stub chains. If a carried stub or
  end item would collide with a foreign item, the **whole call is refused and
  rolled back** (no silent rubber-banding fallback).
- **O9:** PR cut and order as in §13.

## 5. #1 — sheet pins addressable

### 5.1 Addressing

Everywhere a target or endpoint is accepted (`targets.py`, `World.endpoint`), a
sheet pin may be given as:

- its **UUID** (KIID, as returned by `sch_list_items`, `sch_get_pin`,
  `sch_add_sheet_pin`), or
- `"<Sheet name>.<Pin name>"` (today's form; longest sheet name wins, a symbol
  reference beats a sheet name, ambiguity → `ambiguous_pin` — unchanged).

`sch_add_sheet_pin` returns the new pin's UUID (and the child label's UUID) in
its result if it does not already.

### 5.2 `sch_move` on a sheet pin

- New target kind `sheet_pin` in `targets.py` (`Kind`, `_SUPPORTED`,
  `MOVE_KINDS`). The refusal "a sheet pin moves with its sheet symbol" goes away
  for direct targets (moving the sheet symbol still carries all its pins).
- `to` / `by` place the pin; the pin is **snapped onto the nearest edge** of its
  sheet symbol (on-grid, inside the edge's extent, at least one pin pitch from the
  corners). A position that is not near any edge (more than a builder-chosen
  tolerance, author's suggestion: 2.54 mm off the edge) is refused with the
  edges' extents in the error.
- **Side change:** landing on another edge changes the side. The pin's
  orientation (text into the box, v0.4.1 §6 fix) is derived from the side; it is
  never given by the caller.
- Pins may not overlap another pin on the same edge (same rule as
  `_free_pin_y` / `PIN_PITCH`).
- A wire attached to the pin outside the sheet is rubber-banded like a symbol
  pin's wire; own stub chains (v0.4.1 §7 definition) on the parent side are
  carried on a pure translation along the same edge. On a side change the stub is
  rubber-banded (builder may do better; not required).
- The child sheet's hierarchical label is **not** touched (it is on another sheet
  and its position is independent).
- Connectivity: `keep_partition` as for any move.
- `rotate` / `mirror` on a sheet pin: refused ("the side defines a sheet pin's
  orientation; move it to another edge instead").

### 5.3 `sch_delete` on a sheet pin

- Accepts sheet pins (UUID or name form).
- Deletes the pin from the sheet symbol and the wire stub attached to it on the
  parent side if that stub is owned (same rule as for deleting a symbol).
- The matching hierarchical label in the child sheet is deleted **too** if it is
  connected to nothing **and** no other sheet symbol using the same child file
  still has a pin of that name; otherwise it stays and a note says why (the label
  would otherwise dangle as an ERC error; deleting a connected label would change
  the child's nets; deleting a label of a shared file would break the other
  instances — compare `sheets.py:497` for the shared-file case on add).
- `may_change` intent covers the pin's net (as for deleting a label).
- Budget: 1 (§9.1).

### 5.4 Unchanged

- Duplicate names: `plan_add_sheet_pin` keeps refusing a second pin with the
  same name on a sheet (`sheets.py:474`). Renaming a pin is out of scope.

## 6. #2 / #20 — rotate and mirror

### 6.1 Labels

- `sch_move` with `rotate` (90° steps) / `mirror` on local, global and
  hierarchical labels: rotation about the label's anchor point; KiCad's
  `spin_style` / text angle set consistently so the text reads correctly (as the
  KiCad GUI does for "rotate label").
- Mirror on a label = flip its spin style horizontally / vertically (the KiCad
  GUI behaviour); the builder checks what the GUI writes and matches it.
- A label on a wire end stays on that end (anchor unchanged), so connectivity is
  unchanged.

### 6.2 Sheet symbols

- KiCad sheets have no orientation field (kipy `SheetSymbol` has position, size,
  pins only). KiCad's own `SCH_SHEET::Rotate` / `Mirror*` recomputes position,
  size and pin positions / sides. CopperChat does the same through the IPC API:
  rotate the rectangle about its centre (swap width / height for 90°), map every
  pin to its new edge and position, set the pin sides / orientations accordingly.
- The builder compares the resulting `.kicad_sch` text with a sheet rotated /
  mirrored in the KiCad GUI. If the API cannot express it (e.g. pin side
  changes rejected), that is a KiCad patch (§3), not a CopperChat workaround.
- Wires on the sheet's pins: own stub chains are carried (§6.3); other wires
  rubber-band as on a symbol rotation today.

### 6.3 Carrying own stub chains on rotate / mirror (#20)

- Today (`transform.py`, `owned_stub_chains`): chains are carried only on rigid
  translation. New: on rotate / mirror of a symbol (and a sheet symbol, §6.2),
  every **owned** chain (v0.4.1 §7 definition, unchanged) is transformed with the
  same rigid transform as its pin: all wire points and the end items (power
  symbol, label, no-connect) rotate / mirror about the symbol's rotation centre,
  so each chain keeps its shape relative to its pin.
- End items are re-oriented consistently: a power symbol rotates with its stub
  direction (it keeps pointing away from the pin), a label's spin style follows
  the stub direction (text away from the wire), a no-connect is just moved.
- Not owned chains: rubber-banded as today.
- **Collision (O8):** after the transform, if any carried wire or end item would
  overlap / touch a foreign item (another pin, wire, label, field box, sheet edge
  rule) — the same obstacle checks a fresh placement would get — the whole call
  is refused and nothing is written. The error names the pin, the carried item
  and the obstacle (UUID + position) and suggests making room or another angle.
- Connectivity: `keep_partition`.

### 6.4 `sch_move` refusal text

"only symbols rotate or mirror" becomes "rotate / mirror applies to symbols,
labels and sheet symbols". Junctions, no-connects, wires: still refused (no
meaning).

## 7. #4 — `expect_net`

- `sch_move` gets an optional `expect_net: string` (a net name as reported by
  `sch_get_net` / `sch_list_items`).
- Without it, behaviour is unchanged (`keep_partition`).
- With it, the verifier accepts **exactly** this change: **one** connectivity
  group of the moved item(s) is merged with the group of the net named
  `expect_net`, and nothing else changes — no other merge, no split, no other
  net touched. A symbol move where two pins on different nets both land on the
  target net merges three groups → refused (it shorts the two pins).
- Connectivity groups are **item-level**, not pin-only: pins, labels, sheet pins
  and wires, like the existing `joined` groups in `NetIntent` (`changeset.py`).
  Today's pin partition cannot see a floating label (no pins) joining a wire;
  `expect_net` must. `keep_partition` without `expect_net` stays as it is.
- `expect_net` is resolved to its group **before** the move (membership), not by
  name afterwards — the merged net may take the label's name. Anything else → `verification_failed`, compensated and rolled back
  as today. The error names the net that actually resulted ("would also connect
  to X" / "would not connect to Y") so the agent can correct the intent or the
  position.
- If the move produces **no** new connection although `expect_net` was given:
  refused too (the agent's intent was not met) — author's call. (With
  item-level groups the §12 case "floating label onto a wire" is a real merge
  and passes.)
- Unknown `expect_net` name → `invalid_argument` before anything is written.
- Implementation: a new `NetIntent` variant (e.g. `expect_merge_with: str`)
  rather than a flag on `keep_partition`; pre-write route checks (`_short`) must
  allow the declared merge and only that.
- Budget unchanged (a move).

## 8. #12 — ERC

### 8.1 `run_erc`

- New optional `sheet` filter (sheet name or human path); violations whose
  position / items are on that sheet only.
- Every violation carries its stable `id` (`changes/erc.py violation_id`:
  sha1(type + sorted item UUIDs)[:12]) — the same id `plan_step_done` uses, so
  the agent can justify before step end.
- Pagination: `limit` (default 50, max 200) and `offset`; the result carries
  `total` and per-type / per-sheet counts so the agent sees the shape without
  paging everything.
- The cached `state.last_erc` stays the full result (filter / page are views).

### 8.2 Pattern justifications

`plan_step_done.erc_justifications` accepts, besides `{id: reason}` entries, a
list of rules:

```json
{"match": {"type": "pin_not_connected", "sheet": "Power"}, "reason": "..."}
```

- `match` has `type` and / or `sheet` (at least one); exact match on the ERC type
  string and on the sheet as in §8.1.
- A rule justifies every **new** violation it matches. The step result reports
  per rule how many violations it covered.
- Limits: at most 20 rules per call, existing per-id limits unchanged
  (`JUSTIFICATIONS_MAX`, `REASON_MAX`).

### 8.3 Pagination in `plan_step_done`

- The refusal already caps the listed errors (`ERRORS_LISTED = 20`,
  `plans/service.py:54`), but the agent cannot get at the rest except by
  re-running ERC unfiltered. New: the refusal adds `total` and per-type /
  per-sheet counts and points to `run_erc(sheet=…, offset=…)` (which now returns
  the same ids) and to pattern rules. The cap stays 20. The builder also checks
  that the step *result* (`_brief`) does not carry an unbounded delta.

### 8.4 Persistence

- Each step's ERC baseline is the previous step's checkpoint
  (`plans/service.py::_baseline`), so an error justified in step 2 is already in
  step 3's baseline and never needs justifying again. What actually costs the
  agent today is the **retry of the same `plan_step_done`**: a refusal, then
  the whole justification list again.
- So justifications (id and pattern) are stored **per step** (DB, next to the
  existing `erc_results.justifications`) as soon as they are submitted, also on
  a refused call, and apply to every retry of that step's `plan_step_done`; the
  agent sends only what is new. A later call adds to / replaces (same id or same
  match) the stored ones.
- A pattern rule applies **only within the step it was given for**. It does not
  carry to later steps: there it would silently pass brand-new errors, which
  would weaken the ERC gate (anti-goal).
- An id justification lapses by itself when the violation changes (the id is
  derived from type + items).
- The plan card shows each step's justifications (id entries collapsed to a
  count + list, pattern rules with the number of errors they covered).
- Cross-plan / project-wide persistence: non-goal.

## 9. #13 — plan budget and revise

### 9.1 Logical operations (budget unit)

| Tool | Cost |
|------|------|
| `sch_delete` | 1 per symbol (all units), 1 per sheet, 1 per sheet pin; **all wires, junctions, labels, no-connects that are on one net count 1 together per net** (today wires already count per net; labels, junctions, no-connects count 1 each) |
| `sch_move` | 1 per moved primary target (symbol, label, sheet, sheet pin); carried stubs and end items are free |
| everything else | unchanged |

The refund for deleting a symbol placed in the same step (`_step_cost`) stays.
The tool description of each mutating tool states its cost in one line, and the
skill doc gets the table.

### 9.2 `plan_revise` from a running step

- `plans/machine.py`: `plan_revise` is allowed from `running` too. Effect: the
  running step is **paused** (not failed), its changes so far stay recorded and
  counted, the plan goes to `proposed` with revision + 1 awaiting approval.
- On approval: the paused step continues where it stopped. `revise` today
  replaces all unfinished steps with new ids (`machine.py`, kept = done steps);
  the interrupted step therefore becomes the first step of the new revision and
  **inherits its used count and its own-placement ledger** against the new
  expected value. Its edits so far are part of that step (for rollback /
  checkpoint purposes the step's start checkpoint stays the one from before the
  interruption).
- The step counters (`step_used`, `step_placed` in `editing/state.py`) live in
  memory per turn today. Approval starts a new run, so they must be **persisted
  in the DB** with the paused step and restored when it resumes.
- On **reject** of a mid-step revision: the plan falls back to the previous
  revision, still paused at the interrupted step (nothing is discarded, no edit
  is lost); the user can then resume it, roll the step back or cancel the plan
  with the existing actions. (Today reject of a proposed plan ends it; for a
  revision of a plan that already has edits on disk that would strand them.)
- The existing path (step exceeded → failed + paused → revise) stays.
- **Hard stop stays:** exceeding the limit still fails the step and raises
  `plan_step_exceeded`.

### 9.3 Agent guidance

Skill / description: "If you see mid-step that the step needs more changes than
expected, call `plan_revise` *before* exceeding the budget; explain why."

### 9.4 UI: re-post for approval (O7)

- When a plan gets a new revision that needs approval, the frontend renders the
  plan card **again at the bottom of the chat** (as a new chat item at the
  revision's time) with the approve / reject buttons; the earlier card(s) of the
  same plan collapse to one line "superseded by revision N" (click to expand,
  read-only).
- A running step paused for revision shows "paused – revision awaiting approval"
  with the agent's reason.
- Builder's call how the chat item is produced (backend event vs. frontend
  derivation from the plans store); it must survive a reload.

## 10. #3 — finding items

### 10.1 `sch_list_items` (new read tool)

- Inputs (all optional, combined with AND): `sheet`, `types` (subset of
  `label`, `global_label`, `hier_label`, `wire`, `junction`, `no_connect`,
  `sheet_pin`, `power`, `symbol`, `sheet`), `bbox` (`[x1,y1,x2,y2]` mm), `net`,
  `text` (case-insensitive substring / glob on label text, pin name, reference).
  Default `sheet` = the current / root sheet as other read tools do.
- Output per item: `uuid`, `type`, `sheet`, position (`position_mm`, or
  `start_mm` / `end_mm` for wires), `text` where applicable, `net` (name or null
  when unconnected), plus small type-specific fields (sheet pin: `sheet_name`,
  `side`; symbol: `ref`, `value`).
- Pagination: `limit` (default 100, max 500), `offset`, `total`.
- Read-only, in the read-tool allowlists (`agent/profiles.py`).

### 10.2 `sch_get_pin`

- `connected` unchanged (items exactly at the pin), but now also includes sheet
  pins at the point (`Scene.items_at` misses them today).
- New field `stub_end`: if the pin has an own stub chain (v0.4.1 definition),
  the items at its far end (labels, power symbols, no-connects, sheet pins) with
  UUID, type, text, position; plus `stub_wires` (UUIDs). Empty / absent when no
  owned chain.

## 11. #19 — one set of text-extent constants

- `geometry._field_box` uses the measured constants from `fields.py`
  (`CHAR_W=0.93`, `TEXT_PAD=0.1`, `TEXT_H=1.6`), shared from one module; it
  keeps its alignment / rotation handling. (kipy `get_text_extents` was the
  alternative; author's call: constants, since they are measured against this
  KiCad and avoid a round trip per field — the builder may switch if a measurement
  shows the constants are off.)
- Larger boxes change refusals in every tool that uses `field_boxes` (move,
  find_space, overlap checks, placement). Run the whole KiCad suite; update
  goldens only where the new refusal / placement is correct, and list every
  changed expectation in the PR description.

## 12. Acceptance scenario (end-to-end, real KiCad)

One scenario test (`tests/kicad/edit/test_scenarios.py` style):

1. A sheet pin was created on the wrong (left) edge; a label with a UUID unknown
   to the agent sits off the wire.
2. Agent finds both with `sch_list_items`, moves the sheet pin to the right edge
   (`sch_move`, side change), moves the label onto the wire with
   `expect_net` → passes; the same move with a wrong `expect_net` → refused and
   rolled back.
3. No delete-and-recreate anywhere; ERC clean (or justified by a pattern rule).

Further acceptance per item:

- `expect_net`: wanted connection passes; a deviating one is refused and rolled
  back (partition and UUIDs identical to before).
- Budget: deleting a multi-segment wire run with junctions and a label costs 1;
  revise from a running step works and the step continues; exceeding the limit
  still stops hard.
- #20: rotate a symbol with a power stub and a label stub → chains follow; with
  an obstacle in the way → refused, nothing written.
- #19: suite green with the new constants; changed expectations listed.

## 13. PR cut and order (O9)

| Wave | PR | Content |
|------|----|---------|
| 1 (parallel) | A | #19 field-box constants (+ suite regression) |
| 1 (parallel) | B | #13 budget counting, revise from running step, UI re-post (§9) |
| 1 (parallel) | C | #12 ERC (§8) |
| 2 (after A) | D | #3 `sch_list_items`, `sch_get_pin.stub_end` (§10) + #1 sheet pins (§5) |
| 3 (after D) | E | #2 / #20 rotate / mirror labels, sheets, carried stubs (§6) + #4 `expect_net` (§7) + acceptance scenario (§12) |
| 4 | R | Release v0.5.0 |

Merging a sibling: the next PR rebases on main and re-runs CI before its merge
(v0.4.1 lesson). Each PR updates the tool descriptions / schemas and the skill
docs it touches.

## 14. Testing

- Per item: regression tests as in §3, real KiCad where the item touches KiCad
  (#1, #2, #20, #4, #3, #19); unit tests for plan / budget / ERC logic (#13, #12
  in `tests/unit/plans/`, frontend tests for the plan card).
- Description-vs-schema test (from v0.4.1) covers the new / changed parameters.
- CI green.

## 15. Documentation

- Tool descriptions and schemas for every changed / new tool, consistent with
  each other.
- `skills/builtin/kicad-schematic-editing/SKILL.md`: addressing sheet pins,
  `sch_list_items`, rotate / mirror scope, `expect_net`, budget table, revise
  mid-step, ERC patterns. `kicad-review-discipline/SKILL.md`: `run_erc` filter /
  ids.
- User docs on plans (re-posted approval card), CHANGELOG v0.5.0.
- `scripts/check-docs.sh` before the release PR.

## 16. Release

- Minor release **v0.5.0**. Tag only after the release PR is verified MERGED.
- Prod upgrade only with owner OK; back up `data/` with the app stopped.
- Memory hygiene: delete own `review/<PR>/*` project-memory entries after merge.
- Final `pm_report` with a status list for #1, #2, #3, #4, #12, #13, #19, #20.

## 17. Calls made by the author (not owner decisions)

- Sheet-pin snap tolerance 2.54 mm; corner clearance one pin pitch (§5.2).
- Deleting a sheet pin also deletes the child's hierarchical label only if it is
  unconnected and no other instance of a shared child file still uses it (§5.3). (Owner agreed to the proposal "delete the label if it is
  unattached".)
- Side change of a sheet pin rubber-bands its stub instead of carrying it (§5.2).
- Sheet rotate / mirror implemented through position / size / pin updates like
  KiCad's `SCH_SHEET::Rotate` (§6.2).
- `expect_net`: item-level groups, resolved before the move, exactly one moved
  group may join; no resulting connection is refused (§7).
- ERC defaults: page 50 / max 200; max 20 pattern rules; refusal cap stays 20
  (§8). Justifications persisted **per step** across retries; pattern rules do
  not carry to later steps (§8.4 — revised from the "per plan" proposal after the
  review, because later steps' baselines already include justified errors and a
  carried pattern would pass new ones).
- Budget mapping of a revised running step: inherits used count + ledger,
  counters persisted in the DB; reject of a mid-step revision falls back to the
  previous revision, paused (§9.2).
- `sch_list_items` limits 100 / 500 and the type list (§10.1).
- Constants instead of `get_text_extents` (§11).
