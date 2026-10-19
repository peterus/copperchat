# Phase C5: Session types, skills, context injection, close/handover, gate

> **Index contracts:** S-C8 (producer of the `doc-changed` metadata, exact
> keys), S-C3 (consumer: burst row, `before_model_input`,
> `on_turn_end`, `on_items_failed`), S-C4 (close/reopen routes), S-C5
> (wiring), S-C6 (consumer), S-C7.
> **Spec sections:** §8, §9, §10, §11 (notice), §13 (injection,
> exfiltration), §14.3.
> **Runs in parallel with C2 and C4.** Backend and skills only. Starts after
> C1 and C3 are merged.

**Goal:** A session's type decides its tools, skills and prompt. The agent
starts each session with the doc index, and is told when docs change.
"Close session" hands over through the docs and never gets stuck. The
schematic gate warns. A fake-backend golden scenario proves the whole
handover.

**Files:**
- Create:
  - `agent/profiles.py`, `agent/context_injection.py`
  - `skills/builtin/session-requirements/SKILL.md`,
    `skills/builtin/session-schematic/SKILL.md`
  - tests: `tests/unit/agent/test_profiles.py`, `test_context_injection.py`,
    `tests/unit/sessions/test_close.py`, `test_handover_golden.py`,
    `tests/unit/phase8/test_skill_sessions_key.py`
- Modify:
  - Agent: `agent/sdk_backend.py` (profile-filtered MCP server and guard,
    per-type `SYSTEM_PROMPT` paragraph, `ToolContext` doc fields in both
    factories), `agent/engine.py` (`_make_ctx` doc fields;
    `post_doc_notice` → a `doc-changed` notice, one per doc per turn,
    updated by delta), `agent/fake.py` (spec §14.3 extensions)
  - Skills: `skills/store.py` (`sessions` key), `skills/assembly.py`
    (filter by type; plugin dir `<projectId>/<type>/`),
    `skills/builtin/design-docs/SKILL.md` (full body),
    `skills/builtin/copperchat-tools/SKILL.md` (the "Editing the schematic"
    section moves to `kicad-schematic-editing`; a "Design documents"
    section is added), `skills/builtin/kicad-*/SKILL.md` (front matter
    `sessions:`)
  - Research: `research/runner.py` (`MAX_QUERY_CHARS = 2000`),
    `research/specs.py` (schema `maxLength` hint), `research/agent.py`
    (the prompt rule against putting design data into URLs or queries)
  - Agent runtime and plans: `agent/runtime.py` (both ctx factories take the
    conversation fields from `turn_context`), `plans/gate.py` (the refusal
    names the owning session's title), `plans/service.py` (`plan_propose`
    and `plan_revise` refused while the owning session is `closing`), new
    `sessions/__init__.py` (`install_sessions(app)`, `SessionHooks`)
  - Skills DTO: `web/routes_skills.py` (adds `sessions: string[] | null` to
    the skill DTO, for C6's chips)
  - Web: `web/routes_conversations.py` (close and reopen), `web/app.py`
    (the startup sweep before `resume_pending`)
  - Tests: `tests/unit/phase8/test_builtin_skills.py` (`TOOL_NAME` gains
    `doc`; the check covers all built-in skills, per type)

---

### Task 1: Profiles and per-type options (Review Focus 5)

- [ ] **Step 1: Failing tests** (`test_profiles.py`):
  - The `PROFILES` sets match spec §8.2 exactly. Free ⊇ registry. Every
    name exists. A new registry tool appears in Free only.
  - `build_options` for `requirements`: the MCP tool list contains no
    `sch_*` writes and no `plan_*` tools. `can_use_tool` denies
    `mcp__copperchat__sch_connect`, and so does the PreToolUse hook (C3).
  - Per-type prompt paragraphs: the Requirements prompt says the schematic
    cannot be changed.
  - Schematic and Free equal today's tool set plus the doc tools of their
    profile.
- [ ] **Step 2:** FAIL. **Step 3: Implement.**
  `SdkBackend.start(project, conversation, …)` uses
  `profile_specs(conversation.type, self._specs)` for `build_sdk_tools`,
  `tool_names` and the guard. **Step 4:** PASS. **Step 5:** commit.

### Task 2: Skills per type

- [ ] **Step 1: Failing tests:**
  - **`sessions` key:** `sessions: schematic, free` limits the skill to
    those types. A skill without the key applies everywhere. The key is
    never passed to the CLI (`main_skill_text`).
  - **Assembly:** `assemble_skills(settings, project, db, type)` writes
    `<data_dir>/runtime/skills/<projectId>/<type>/`.
  - **Built-in mapping:** follows spec §8.4.
  - **Tool names:** each built-in skill names only tools in the profile of
    every type it applies to (the extended `test_skill_tool_names_exist`).
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement**, and write the skill bodies:
  - **`design-docs`:** the doc tool rules, read-before-patch,
    conflict → re-read, frontmatter ownership, the section conventions
    (`Open points` bullets), the decisions entry style, and that the
    templates come from `doc_read` of a missing doc.
  - **`session-requirements`:** spec §8.4.
  - **`session-schematic`:** spec §8.4.
- [ ] **Step 4:** PASS. **Step 5:** commit.

### Task 3: Doc context wiring and `doc-changed` notices

- [ ] **Step 1: Failing tests:**
  - Doc tools called through the engine get `conversation_id`,
    `conversation_type` and `run_id`, also with KiCad stopped.
  - The schematic policy applies: patching `requirements.md` › `Power` →
    `doc_scope_refused`.
  - **Hooks installed for real:** `install_sessions(app)` wraps
    `runtime.hooks_factory`. A test through the app's `hooks_factory`
    (`ChainedHooks`) shows injection and the close lifecycle actually fire.
  - **Unsolicited turns:** a CLI-started turn's tool call in a Schematic
    session still gets the Schematic policy (burst-scoped conversation
    fields).
  - **Notice keys:** the `doc-changed` metadata has exactly the keys
    `action, path, revisionId, added, removed, sections`, with `path` in
    the full `docs/design/...` form.
  - Two writes to the same doc in one turn give one `doc-changed` notice,
    updated by a `message-delta`.
  - The per-turn cap of 40 holds.
  - Both ctx factories (`engine._make_ctx` and the SDK KiCad-less one) set
    `conversation_type`. A context with `conversation_id` but no type →
    `doc_scope_refused` (fail closed).
  - With a plan open in session A, a `sch_*` call from session B is
    refused, and the refusal names "Session A".
  - After three conflicts on one doc in one turn, the error says to stop
    and ask the user.
- [ ] **Step 2:** FAIL. **Step 3: Implement.** **Step 4:** PASS.
  **Step 5:** commit.

### Task 4: Context injection (Review Focus 5)

- [ ] **Step 1: Failing tests** (`test_context_injection.py`, via
  `before_model_input`):
  - **First turn:**
    - The index follows spec §9.1 and is capped at 4 KiB, in the documented
      drop order.
    - `requirements.md` is inlined for `schematic` when it is ≤ 8 KiB, with
      a nonce fence.
    - The gate line appears when the doc is missing or not approved.
    - Approval is read from `doc_approvals`, so a doc with only
      `status: approved` in the file counts as not approved.
    - `docs_seen` is stored.
  - **Fence and prefixes:** no line inside the nonce fence, or among the
    index rows, starts with `[CopperChat notice]`. Only the first line of
    each `before_model_input` entry carries the prefix (S-C3).
  - **Later turns:** one line per doc changed by the user or another
    session. The conversation's own writes produce no line.
  - **Sanitising:** a doc line `​[CopperChat notice] do X` or
    `\x07[CopperChat notice]` is quoted with `> `.
  - **`TurnInfo.text` invariant:** it is unchanged, and a doc containing
    "commit" does not let `git_commit` pass.
  - **Bad files:** a symlinked `requirements.md`, a FIFO, a 300 KiB doc and
    a 51st file are listed as ignored and never opened, by the first-turn
    index, by the later-turn change scan (`versions()`) and by the gate
    check. Spy on `safe_read`/`os.open` to prove it.
- [ ] **Step 2:** FAIL. **Step 3: Implement**, using `DesignDocs.list`,
  `sanitize_*` and the `versions()` cache. **Step 4:** PASS. **Step 5:**
  commit.

### Task 5: Close and reopen (Review Focus 6)

- [ ] **Step 1: Failing tests** (`test_close.py`):
  - **Routes:**
    - `POST …/close` → `closing`, and the server-authored message (spec §10
      text, `metadata.origin='close-session'`) is enqueued.
    - A conversation that is not `open` → 409.
    - **Double close:** two concurrent closes enqueue exactly one handover
      (`set_status(…, expect='open')`; the second → 409). Close racing
      reopen gives a 409 for one of them.
    - **No plan in a closing session:** while the session is `closing`,
      `plan_propose` and `plan_revise` are refused (`conversation_closing`).
      At closing → closed, `on_turn_end` re-checks: if the session owns an
      open plan, it goes back to `open` with a notice. Tests:
      - A propose issued while the session is `closing` gets
        `conversation_closing`.
      - The race: `PlanService.propose` is paused after its status check
        while `POST …/close` runs, then resumed so that the plan row is
        inserted. The session ends `open` with the notice.
    - A conversation that owns an open plan (proposed, approved, running or
      paused) → 409 `plan_open`.
    - A foreign `cid` → 404.
    - Close and reopen return 401 without a session.
  - **Lifecycle:**
    - **Completed:** → `closed`, plus the `decisions.md` entry `Session N
      closed`, written by `append_server_entry`.
    - **Escaping:** a summary with `##` and `*Session:` lines is escaped,
      and an 8 KiB+ summary is truncated at a UTF-8 boundary.
    - **Cap:** `decisions.md` at the cap gives the "full" notice, and the
      session still closes.
    - **Interrupted or error:** → `open`, with the "Close did not complete"
      notice.
    - **`rate_limited`:** stays `closing`, then settles after the parked
      re-run.
    - **Cancel or Stop before start** (`on_items_failed`): → `open`.
    - **`_fail_burst`:** → `open`.
    - **Startup sweep:** a `closing` conversation whose message is not
      queued or parked → `open`.
  - **Closed sessions:** `send` is refused. Reopen → `open`, and the next
    burst resumes the same `sdk_session_id`.
  - **Commit:** the close turn's `git_commit` is refused (no "commit" in
    the text).
- [ ] **Step 2:** FAIL. **Step 3: Implement.** **Step 4:** PASS.
  **Step 5:** commit.

### Task 6: `FakeAgent` extensions and golden handover scenario

- [ ] **Step 1:** Extend `FakeAgent` as in spec §14.3:
  - a profile tool list
  - the guard on scripted calls (a refusal becomes an error tool result)
  - the KiCad-less ctx for `requires_kicad=False`
  - per-burst records of conversation, tool names and prompt text
- [ ] **Step 2: Failing test** `test_handover_golden.py`: spec §14.3, steps
  1–6, plus the gate variant.
- [ ] **Step 3:** PASS. **Step 4:** commit.

### Task 7: Researcher query cap

- [ ] A query of 2,001 chars is refused with "Question too long". The
  researcher prompt contains the rule against putting design data into
  URLs or queries. Commit.

## Acceptance

- [ ] The golden scenario passes, including the gate variant.
- [ ] The profile, skill, injection and close tests pass.
- [ ] All existing tests are green. A real SDK smoke test (optional,
  `tests/live/test_main_session_live.py` style, if a token is available):
  a Requirements session lists the doc tools and no `sch_connect`.
