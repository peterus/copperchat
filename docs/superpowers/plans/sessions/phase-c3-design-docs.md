# Phase C3: Design docs core (guard, service, tools, REST, hook)

> **Index contracts:** S-C1 (`0009`), S-C4 (docs routes, `doc-updated`),
> S-C5, S-C6.
> **Spec sections:** §4.2 (doc tables), §6.1 (docs routes), §6.2
> (`doc-updated`), §7, §13 (path guard, frontmatter, REST).
> **Runs in parallel with C1.** Do not touch `agent/engine.py`,
> `agent/runtime.py`, `db/repos/messages.py`, `web/ws.py` or the chat store.
> In `agent/sdk_backend.py`, only the hook wiring of Task 6 is allowed.

**Goal:** A `DesignDocs` service with a path guard that cannot be escaped,
safe readers, server-owned frontmatter, body-hash versions, approvals in the
DB and revisions. On top of it: five KiCad-less doc tools, the `/docs` REST
API and the `doc-updated` frame. The main agent gets a PreToolUse hook that
mirrors its guard.

**Files:**
- Create:
  - `db/migrations/0009_design_docs.sql`
  - `design_docs/{__init__,paths,frontmatter,sections,service,repo,specs,sanitize}.py`
  - `web/routes_docs.py`
  - `skills/builtin/design-docs/templates/{requirements,architecture,parts,decisions,board}.md`
  - `skills/builtin/design-docs/SKILL.md` (a minimal valid skill: name,
    description, a short rules section; C5 completes the body)
  - `protocol-fixtures/doc-updated.json`
- Modify:
  - `tools/spec.py` (S-C5 fields), `tools/registry.py` (provider
    `copperchat.design_docs.specs`), `fsutil.py` (no-follow directory
    check in `atomic_write_text`)
  - `agent/guards.py`, `agent/sdk_backend.py` (hook wiring; `run_id` in the
    KiCad-less ctx)
  - `agent/protocol.py`, `frontend/src/protocol/frames.ts` (`doc-updated`
    only)
  - `web/app.py` (router plus `install_design_docs`)
- Tests: `tests/unit/design_docs/test_paths.py`, `test_frontmatter.py`,
  `test_sections.py`, `test_service.py`, `test_tools.py`, `test_routes_docs.py`,
  `test_sanitize.py`; `tests/unit/agent/test_main_hook.py`

---

### Task 1: Path guard and safe readers (Review Focus 3)

- [ ] **Step 1: Failing table tests** (`test_paths.py`), in a temp project.
  - **Refused raw paths** (`doc_path_refused`):
    - `..`, `/etc/passwd`, `docs/design/../x.md`, a NUL, a backslash
    - `Docs/design/a.md`, `docs/design/A.md` (uppercase)
    - `docs/design/.hidden.md`, `docs/design/a/b/c.md` (depth 3)
    - `docs/design/a.txt`, `docs/design/a.md\n` (trailing newline)
    - a control character, a 201-char path
  - **Accepted:** the bare name `requirements.md` maps to
    `docs/design/requirements.md`.
  - **Refused for read and write:** a symlinked `docs`, `docs/design` or
    intermediate directory, a symlinked target (→ `/proc/self/environ`), a
    hard link (write only), a FIFO (the read does not block; 1 s timeout),
    and a file over 256 KiB.
  - **Race:** replace the target with a symlink between check and open.
    The read fails with `O_NOFOLLOW`.
  - **Walk:** it lists at most 50 docs, at depth ≤ 2, and does not follow
    directory symlinks. Rejected entries come back as `ignored` with a
    reason.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement** `resolve_doc_path`, `safe_read` and `walk`
  exactly as in spec §7.5:
  - lexical `lstat` of each component
  - `os.open(O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_NOCTTY)`
  - `fstat` with `S_ISREG` and the size cap; read at most cap + 1 bytes
  - segment `fullmatch`

  Writes: temp file plus `rename` in the same directory, with a no-follow
  check of the directory just before. The whole scan runs in one
  `asyncio.to_thread`.
- [ ] **Step 4:** PASS. **Step 5:** commit.

### Task 2: Frontmatter, sections, sanitiser

- [ ] **Step 1: Failing tests.**
  - **Frontmatter:** the server rebuilds it, and keeps unknown keys
    verbatim. A body starting with `---` → `doc_frontmatter_refused`. A
    `---` later in the body is kept.
  - **Sections:**
    - ATX levels 1–4, fenced code ignored
    - duplicate headings → `doc_section_ambiguous`, resolved by the
      `"Parent/Child"` path
    - missing heading → `doc_section_not_found` with the list; `create=True`
      appends a level-2 heading
    - `mode=append`
    - CRLF preserved
    - headings in the body at level ≤ target → `doc_heading_refused`;
      deeper ones allowed
  - **Sanitiser:**
    - `sanitize_inline` removes Cc (except tab and line breaks) and Cf,
      splits on all Unicode line breaks, strips leading whitespace, and
      prefixes `> ` to lines that start with `[CopperChat notice]`
      (case-insensitive)
    - `sanitize_field` gives one line, ≤ 80 chars
- [ ] **Step 2:** FAIL. **Step 3: Implement.** **Step 4:** PASS. **Step 5:**
  commit.

### Task 3: Service — versions, conflicts, revisions (Review Focus 4)

- [ ] **Step 1: Failing tests** (`test_service.py`):
  - **Versions:**
    - The version is the body hash. A frontmatter-only change keeps the
      version.
    - A stale `base_version` → `doc_conflict` with `current_version`, the
      author of the newest revision, and a diff from base when the base is
      in `doc_revisions` (≤ 8 KiB). Nothing is written.
    - A user PUT is interleaved between the agent's read and its write
      (under the lock): the agent gets a conflict.
    - A size- and mtime-preserving external write is detected: the conflict
      check hashes the actual bytes.
  - **Revisions:**
    - A new external version produces an `author='external'` revision on
      first sight.
    - `added` and `removed` are stored. `revisions()` runs no `difflib`
      (patch it to raise).
    - Retention: 100 per doc and 32 MiB per project, and the approved body
      version is kept.
  - **Caps:** every cap from spec §7.4.
- [ ] **Step 2:** FAIL. **Step 3: Implement** S-C6, including the per-turn
  mutation counter keyed by `(project_id, run_id)`. The write-time
  `difflib` runs in `to_thread`, with counts only above 4,000 lines.
- [ ] **Step 4:** PASS. **Step 5:** commit.

### Task 4: Approval, user_edited, policies, decisions

- [ ] **Step 1: Failing tests:**
  - **Approval:**
    - `approve(version)` stores `doc_approvals` and creates no revision. A
      stale version → conflict.
    - Approved iff the row exists and its `body_version` equals the current
      version.
    - A hand-written `status: approved` in the file is not approved.
    - An agent change to an approved doc → draft, frontmatter rewritten,
      and a `DocNotice(kind='approval_lost')` is returned.
  - **`user_edited`:**
    - A user write sets it.
    - `doc_write` → `doc_user_edited`, while patch and append still work.
    - `clear_user_edited(version)`.
  - **Policies:**
    - `decisions.md`: `doc_write` and `doc_patch_section` give
      `doc_append_only`. Append needs a title, the entry format matches
      spec §7.4, and the text may not contain a heading of level ≤ 2 or a
      `*Session:` line.
    - `append_server_entry` escapes, then truncates at a UTF-8 boundary to
      8 KiB with a marker. At the file cap it returns `None`.
    - `policy_for('schematic')` allows only `decisions.md` appends and the
      `Open points` / `Key parts` sections. Everything else gives
      `doc_scope_refused`. `requirements` cannot write `board.md`.
      `conversation_type=None` behaves like Free.
  - **Templates:** resolved per file, project → user → built-in. A
    project-layer `design-docs` without `templates/` still finds the
    built-in templates. A disabled skill still serves its templates.
- [ ] **Step 2:** FAIL. **Step 3: Implement.** **Step 4:** PASS. **Step 5:**
  commit.

### Task 5: Doc tools, REST, `doc-updated`

- [ ] **Step 1: Failing tests:**
  - **Tools** (`test_tools.py`, a `ToolContext` with
    `session=None`-style KiCad-less context): all five tools follow spec
    §7.4 signatures and results. `doc_read` of a missing fixed doc returns
    the template. The tools are `requires_kicad=False` and `mutating=False`.
    The registry exposes them.
  - **REST** (`test_routes_docs.py`):
    - every route from spec §6.1
    - route order: each suffix route reachable, and `/docs/x.md/revisions`
      is not served as a doc
    - 409 bodies; a foreign `rid` → 404; a 1 MiB body cap; the file cap on
      bytes; no absolute paths in errors
  - **Frame:** a `doc-updated` frame is published on every write, approve,
    unapprove and clear. The fixture round-trips, with
    `conversationId` omitted for user edits.
- [ ] **Step 2:** FAIL. **Step 3: Implement.** The frontend `frames.ts`
  gains the `doc-updated` schema.
- [ ] **Step 4:** PASS. **Step 5:** commit.

### Task 6: Main-agent PreToolUse hook (Review Focus 5)

- [ ] **Step 1: Failing tests** (`test_main_hook.py`):
  - `build_options` sets `hooks={"PreToolUse": [HookMatcher(matcher=None,
    hooks=[guard_as_pre_tool_use_hook(make_main_guard(allowed))])]}`. The
    hook's guard has **no** `on_allowed`.
  - An out-of-list tool is denied by the hook.
  - One `research_part` call, seen by both `can_use_tool` and the hook, is
    recorded once (`research.note_call` called once).
  - The KiCad-less ctx carries `run_id`.
- [ ] **Step 2:** FAIL. **Step 3: Implement.** **Step 4:** PASS.
  **Step 5:** commit.

## Acceptance

- [ ] All C3 tests pass. The path table proves there is no access outside
  `docs/design/`, for every reader.
- [ ] Under the current (single-conversation) engine, the Free agent can
  use the doc tools end to end. Smoke test: a fake-agent script calls
  `doc_write`, then `doc_read`.
- [ ] Existing tests are green, and `test_skill_tool_names_exist` still
  passes (the new `design-docs` skill names only doc tools).
