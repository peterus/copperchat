# Phase P9b — Board skills, prompt, live eval, docs, flag default on

> Index contracts: B-C1 (`board_tools` default → `True`), B-C10 (checkpoint semantics the
> skill explains), B-C14 (error codes the skill explains); index calls 5, 9, 10. Spec §6.5
> (prompt, profiles, `board.md` migration), §7 (tool list), §8 (flow, questions, checkpoints,
> `board.md` v2), §9.3 (part-class rules), §10.1–§10.3 (critical set, ladder), §11, §12
> (thresholds, accepted warnings), §15.3 D1–D5, §18.4 (agent eval). **Wave 10: starts when
> P9a is merged.**

**Goal:** the agent runs the S0–S9 board flow the owner asked for — proposes values and asks,
stops after every step with images and a check summary, never routes before the placement
approval, never calls the board done with open connections — and the feature is documented
and switched on by default. **The release itself is not part of P9b:** after this PR is
merged, the coordinator cuts the release with the release recipe (version bump, CHANGELOG
section, tag, `release.yml`) as a separate step.

`cc/` = `backend/src/copperchat/`.

**Files:**
- Create: `cc/skills/builtin/board-layout/SKILL.md`, `backend/tests/live/test_board_eval.py`,
  `backend/tests/unit/board/{test_board_skills,test_board_prompt_final}.py`
- Modify: `cc/skills/builtin/session-board/SKILL.md` (P1c skeleton → full),
  `cc/skills/builtin/copperchat-tools/SKILL.md`, `cc/agent/sdk_backend.py` (prompt text),
  `cc/config.py` (`board_tools` default `True`), tests that assumed the old default
  (`tests/unit/test_config.py` and every board test that relied on "off" — they now set it
  explicitly), `README.md`, `.env.example`, `docs/architecture.md`, `CHANGELOG.md`

---

### Task 1: `session-board` — the full S0–S9 flow

**Interfaces:** `session-board/SKILL.md` keeps P1c's frontmatter, rules and the ten step
headings; each step's "Full content: P9b." is replaced by: **Do** (tools in order with the key
arguments), **Show** (step report: at most **3 images**, a short check summary — what passed,
what needs attention —, the decisions taken, one question), **Then stop the turn and wait**
for the user's ok or corrections. Content per spec:
- **Rules:** propose a value with its reason and ask for confirmation instead of open
  questions (look in `requirements.md`, `architecture.md`, mechanical files first); record
  answers in `board.md` with `doc_patch_section`; the schematic is read-only here — a needed
  schematic change (wrong footprint, missing pin) is written to `board.md` → Open points and
  the user is asked to make it in a Schematic session, then `pcb_sync_from_schematic` again;
  `pcb_status` at the start of every turn; re-entering an earlier step makes later checkpoints
  stale (hashes).
- **S0 Intake:** the 8 design questions of spec §8.2, each with when to ask it (1 size/shape —
  from a case document, else propose from the summed courtyard area at ≤ **45 %** fill:
  `area = Σ courtyard / 0.45`, rounded up to whole mm, aspect from the connector edges; 2 layer
  count with a reason, default proposal Sig/GND/Sig+Power/Sig for 4 layers (D1), never
  hard-coded; 3 fab + assembly → allowed sides, written as `Assembly: <method>; sides:
  top|top+bottom` in Summary (index call 9; hot plate → top); 4 edges; 5 mounting holes;
  6 keepouts/height limits; 7 supply currents (at S2); 8 special nets (at S3 after
  `pcb_analyze`)); ERC-clean and every symbol with a footprint (local footprints approved).
- **S1–S3:** sync report with warnings explained (`fp_check` hint); outline + rules table;
  blocks + floorplan render.
- **S4 Placement (hard checkpoint):** connectors/fixed parts first (`pcb_move` + `locked`),
  then `pcb_place_block` per block, `pcb_legalize`, `pcb_metrics` until M1/M8/M9/M10 pass or
  each exception is shown; `does_not_fit` → ask (bigger board, bottom side), never overlap;
  evidence `pcb_render {view: "placement"}` + `pcb_check {level: "quick"}` with M1 passing;
  question **"Approve this placement?"**; `pcb_approve {stage: "placement"}` only in a **later**
  turn, with `user_quote` = the user's own words from that message (≥ 2 words), never while a
  plan runs; any move after the approval needs a new approval — batch moves, show, ask again.
- **S5 Critical routing:** confirm the critical set (spec §10.1 default) with the user, then
  `pcb_route` (locked).
- **S6 Fill:** `pcb_route_fill`; the completion ladder (a) more `passes`, (b) `pcb_unroute
  {nets: [critical signal nets next to the open connections], unlock_only: true}` then
  `pcb_route_fill {relock_nets: […]}` — critical signal nets only, never ground/plane-net items,
  (c) `pcb_route` the rest, (d) report open connections with positions and ask (placement
  change, more layers, larger board); `fill_not_acceptable` → improve channels or the
  critical routing, then retry. **Never call the board done with open connections.**
- **S7 Planes:** `pcb_zones {action: "add"}` (default stack per layer count), `pcb_fanout`,
  `pcb_stitch`, per-layer renders on request only.
- **S8 Final check (hard checkpoint):** `pcb_tidy_silk`, `pcb_check {level: "full"}`, fix
  loop; a DRC warning becomes an accepted row in `board.md` → Checks (`| DRC type |
  Items/refs | Reason | Accepted by |`) **only after the user accepted it** in a message;
  per-footprint DFM exceptions via `pcb_set_rules.custom_rules` with a reason — never a
  weaker global rule; evidence `pcb_render` top + bottom; question **"Approve this board for
  manufacturing?"**; `pcb_approve {stage: "board"}` with the same rules as S4.
- **S9 Fab:** `pcb_fab_export {preset: "jlcpcb"}`, file list + order notes, offer a git commit.
- **`board.md` v1 → v2:** when `## Stack-up` / `## Constraints` (v1) exist, move their content
  into `## Stack-up and rules` / `## Mechanical` + `## Routing` with `doc_patch_section` and
  leave the v1 sections empty with a pointer (no `doc_write`).
- **Error codes** and what to do: every B-C14 code reachable from a `pcb_*` tool.

- [ ] **Step 1: Failing unit tests** (`test_board_skills.py`): `test_session_board_complete`
  (10 step headings in order, no "Full content: P9b."); `test_eight_questions` (one numbered
  item per §8.2 question, keywords: size, layer, assembly, edge, mounting, keepout, current,
  special nets); `test_rules_present` ("45 %", "at most 3 images", "stop the turn and wait",
  "propose", both checkpoint questions verbatim, "Never call the board done with open
  connections", ladder (a)–(d), "only after the user accepted"); `test_session_board_tools_exist`
  (every `pcb_*` / `fp_*` / `doc_*` name in the text is registered and in the `board`
  profile with `board_tools=True`); `test_every_board_error_code_explained` (each B-C14 code a
  `pcb_*` tool can raise appears); `test_no_design_specific_names` (no `[A-Z]{1,3}\d{3}` ref,
  no "OpenDriveHub"/"ODH").
- [ ] **Step 2: Run** `uv run pytest tests/unit/board/test_board_skills.py -q` — FAIL.
  **Step 3: Write.** **Step 4: Run** — PASS (also `tests/unit/phase8/test_builtin_skills.py`).
- [ ] **Step 5: Commit** `docs(board): session-board skill with the full S0–S9 flow`.

### Task 2: `board-layout` skill and `copperchat-tools`

**Interfaces:** `board-layout/SKILL.md` (frontmatter `name: board-layout`, description "Rules
for placing and routing a board by part class …"): (1) the spec §9.3 table — part class →
rule → checked by (M8, M9, M12, M13, DRC), generic, plus crystal ≤ 5 mm and the decoupling
"smallest value closest, ground via next to the pad"; (2) routing rules — critical set default
(decoupling, switch node/hot loop, crystal, differential pairs, 2-pin nets with airwire
≤ 10 mm), own router for those (locked), Freerouting for the rest, ground by planes not
tracks; only 0°/45° direction changes; the chamfer / too-short chamfer / micro-jog
definitions verbatim from spec §12 notes; pad entries are not bends; (3) plane/fanout rules —
D1 default stack, 2-layer GND on B.Cu, outer pours below planes, fanout stub ≤ 1 mm at 45°/90°,
no via-in-pad (fine-pitch pads via the outer pour), stitching default pitch 5 mm, refill after
every zone change; (4) the owner's hard thresholds (D2) verbatim from the index Global
Constraints and the review thresholds of spec §12, "hard thresholds change only with the
owner's OK". `copperchat-tools/SKILL.md`: new `## Board` section listing `PCB_READ` and
`PCB_WRITES` by group (read; setup; placement; routing; copper; checkpoints and output), one
line each, "load `session-board` in a Board session and `board-layout` before placing or
routing"; the footprint paragraph's "Writing tools … exist only in Schematic and Free
sessions" becomes "… in Schematic, Board and Free sessions" (P1c added `FP_WRITES` to Board).

- [ ] **Step 1: Failing unit tests** (`test_board_skills.py`): `test_board_layout_classes`
  (every `PartClass` literal of `cc/board/classify.py` except `other` appears);
  `test_board_layout_hard_thresholds_verbatim` (the M4, M5, M7, M8 hard lines of the index
  Global Constraints appear verbatim); `test_board_layout_definitions` (`max(1.0 × width,
  0.25 mm)`, `2 × width`); `test_copperchat_tools_lists_board_tools` (every registered
  `PCB_READ`/`PCB_WRITES` name appears); `test_fp_writes_in_board_sentence`;
  `test_no_design_specific_names` extended to `board-layout`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Write.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `docs(board): board-layout skill and board tools in copperchat-tools`.

### Task 3: Prompt final, flag default on

**Interfaces:** `cc/config.py`: `board_tools: bool = Field(default=True,
validation_alias="COPPERCHAT_BOARD_TOOLS")`. `sdk_backend.py`: the board entry of
`SESSION_PARAGRAPHS` and the board part of `_EDITING` (P1c's flag-on text) become final: the
board is laid out step by step with the `pcb_*` tools; load `session-board` first and
`board-layout` before placing or routing; stop after every step and wait; two server-enforced
approvals (placement before routing, board before fab); the schematic stays read-only. The
flag-off text stays exactly P1c's.

- [ ] **Step 1: Failing unit tests:** `test_config.py::test_board_tools_env` → unset `True`,
  `COPPERCHAT_BOARD_TOOLS=off` → `False`; `test_board_prompt_final.py`:
  `test_prompt_mentions_both_skills`, `test_prompt_states_checkpoints`,
  `test_flag_off_text_unchanged` (P1c literal); `test_default_profiles_have_board_writes`
  (default `Settings()` → board and free profiles contain every registered `PCB_WRITES` tool;
  `POST /conversations type=board` → 201, `/api/features` → `{"boardTools": true}`).
- [ ] **Step 2: Run** `uv run pytest -m "not kicad" -q` — FAIL (new tests) and list every
  existing test that assumed the old default; make those set `board_tools=False` explicitly
  where they test the off state. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): board tools on by default; final board prompt`.

### Task 4: Live agent eval (spec §18.4)

**Interfaces:** `backend/tests/live/test_board_eval.py`, `pytestmark = pytest.mark.live_sdk`,
opt-in `COPPERCHAT_LIVE_EVAL=1` and `LIVE_EVAL_MAX_USD` as `test_editing_eval.py`; real
`SdkStreamingBackend`, real registry and built-in skills, a Board session on a copy of
`sp4_synth` (inside the kicad-base image, Freerouting not needed). Script: turn 1 "Lay out the
board for this project." → turn 2 answers the S0 questions with fixed values → continue (at
most 8 turns) until the agent asks "Approve this placement?" → stop (no approval given).
Writes `tests/live/results/<date>-board.json` (tool calls, tokens, cost, verdicts).

- [ ] **Step 1: Write the eval** with assertions: `test_asks_s0_questions` (turn 1's final
  text asks about at least size, layer count, assembly/sides, connector edges, mounting holes —
  keyword match, and no `pcb_*` write tool was called before the user answered);
  `test_stops_after_placement` (the turn that shows the placement render ends with a question
  and calls no routing tool); `test_never_routes_before_approval` (no `pcb_route`,
  `pcb_route_fill`, `pcb_zones`, `pcb_fanout`, `pcb_stitch` call succeeds in the whole run; any
  attempt was refused `placement_not_approved` — the server guard makes this deterministic);
  `test_step_reports_at_most_3_images`.
- [ ] **Step 2: Run once** `COPPERCHAT_LIVE_EVAL=1 uv run pytest -m live_sdk
  tests/live/test_board_eval.py` in the kicad-base image with credentials (`timeout 1800`);
  record results and cost in the PR. Not CI-gating.
- [ ] **Step 3: Commit** `test(board): live board-session eval (S0 questions, stop after placement)`.

### Task 5: README, `.env.example`, architecture, CHANGELOG

- [ ] **README:** feature section "Board layout" (what the agent does S0–S9, the two
  approvals, JLCPCB package in `fab/jlcpcb/`, 3D export bare until P10, Freerouting runs
  locally as a separate GPL-3 process with no network access); configuration rows
  `COPPERCHAT_BOARD_TOOLS` (`on` default / `off` removes the board write tools and the Board
  session type), `COPPERCHAT_FREEROUTING` (`on` default / `off` removes only
  `pcb_route_fill`), `COPPERCHAT_FREEROUTING_HOME` (default `/opt/freerouting`, the image ships
  it); host memory: **≈ 4.5–5 GB per active board project** (owner D5) with P9a's measured
  breakdown (api-server with board, footprint libraries, Freerouting heap ≤ 1 GB).
- [ ] **`.env.example`:** `# COPPERCHAT_BOARD_TOOLS=on` (replacing P1a's commented `off`),
  `# COPPERCHAT_FREEROUTING=on`, `# COPPERCHAT_FREEROUTING_HOME=/opt/freerouting`.
- [ ] **`docs/architecture.md`:** db row `0001`–`0013`; new `## Board` section (pure core
  `board/`, KiCad layer `kicad_tools/board/`, `BoardChangeSet` + KIID/full-copper compensation,
  CPU worker, Freerouting runner + post-processor, checkpoints `board_checkpoints` /
  `board_reviews` (migration 0013), fab path helper, live board view, tests incl. the
  `kicad-e2e` job); CI section names the `kicad-e2e` job.
- [ ] **`CHANGELOG.md` `[Unreleased]`:** replace the per-phase "behind COPPERCHAT_BOARD_TOOLS"
  lines with one "### Added — Board layout" entry (tools, flow, approvals, fab package,
  Freerouting, flags, memory note) and "### Changed — app image ships Freerouting 2.5.0 + Temurin
  25 JRE".
- [ ] Run `bash scripts/check-docs.sh` (from the repo root) → `OK`; unit test
  `test_board_skills.py::test_readme_lists_board_env` (the three variables in README and `.env.example`).
- [ ] **Commit** `docs(board): README, architecture, CHANGELOG for board layout`.

### Task 6: Index as built, checks, PR

- [ ] Index "As built (P9b)": final prompt text location, live eval result and cost, the
  Residuals list completed from every phase's "As built", B-C1 default updated in place.
- [ ] Run `uv run pytest -m "not kicad"`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy src`, `npm run lint`, `npx vitest run`, `npm run typecheck`, `uv run pytest -m
  "kicad and not kicad_e2e" tests/kicad/board` and `-m kicad_e2e` in the kicad-base image,
  `bash scripts/check-docs.sh` (all with `timeout`).
- [ ] PR `board phase P9b: board skills, docs, flag on`; review-loop per the index's Phase
  Conventions. Done report to the coordinator names the release as the next (coordinator)
  step.

## Acceptance (P9b)

- [ ] CI `backend`, `frontend`, all `kicad (…, i/8)` shards, `kicad-e2e` and `e2e` green;
  `scripts/check-docs.sh` OK.
- [ ] `test_session_board_complete`, `test_rules_present`, `test_board_layout_hard_thresholds_verbatim`,
  `test_copperchat_tools_lists_board_tools`, `test_default_profiles_have_board_writes` pass.
- [ ] A default install offers the Board session type and all `pcb_*` tools;
  `COPPERCHAT_BOARD_TOOLS=off` restores the pre-SP4 surface.
- [ ] Live eval run once, results and cost in the PR (asks S0 questions, stops after placement,
  no routing before approval).
- [ ] README documents the three variables and ≈ 4.5–5 GB per active board project (D5).
- [ ] Release not cut here — handed to the coordinator's release step.
