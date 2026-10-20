# CopperChat Sessions & Design Docs Implementation Plan (index)

> **For agentic workers:** REQUIRED SUB-SKILL: use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task by task. Steps use
> checkbox (`- [ ]`) syntax for tracking.
>
> This file is the **index**. Each phase has its own file under
> `docs/superpowers/plans/sessions/`, and one child session executes it as
> **exactly one PR**.
>
> A phase executor reads, in this order: this index, the spec, then its own
> phase file. The foundation index (`2026-10-15-foundation-plan.md`) and the
> editing index (`2026-10-18-editing-plan.md`) stay binding for everything
> this plan does not change.

**Goal:** Build sub-project 3, "Sessions & design docs":
- several sessions (conversations) per project, with one active run per
  project
- session types as tool profiles plus skills
- guarded design docs under `docs/design/`, with doc tools
- a Docs panel
- a close-session handover and a soft phase gate
- release v0.3.0

**Architecture:**
- **Engine.** One `RunEngine` per project stays the run lock. Each burst
  binds to one conversation, through sealed per-conversation `PushStream`s.
- **Conversations.** The SDK session id moves from `agent_sessions` to
  `conversations`.
- **Doc tools.** They are KiCad-less, non-mutating MCP tools over a
  `DesignDocs` service:
  - a path guard and a server-owned frontmatter
  - a body-hash version, approvals in the DB, and full-content revisions
- **Profiles.** Tool profiles filter the MCP server per burst. They are
  enforced by `can_use_tool` and a main-agent PreToolUse hook.

**Tech stack:** unchanged from SP2:
- Python 3.13, FastAPI, aiosqlite
- `claude-agent-sdk==0.2.168`
- React 19, zustand, zod

**Spec:** `docs/superpowers/specs/2026-10-19-sessions-design.md`. It was
approved on 2026-10-19, and §17 lists the owner decisions. Section numbers
below (§n) refer to the spec.

---

## Phase overview

| # | Phase file | Goal (one line) | Depends on |
|---|---|---|---|
| C1 | `sessions/phase-c1-conversations-core.md` | migration `0008`, `ConversationRepo`, the engine bound to conversations (sealed streams, scoped Stop, notes and notices), REST `/conversations*` (without close), WS frame changes and fixtures, restore `conversationId`, interim frontend, build-version banner | — |
| C3 | `sessions/phase-c3-design-docs.md` | migration `0009`, the `design_docs` package (path guard, safe readers, frontmatter, sections, versions, approvals, revisions), five doc tools, REST `/docs*`, `doc-updated` frame, new `ToolContext` fields, main-agent PreToolUse hook, templates | — (parallel to C1) |
| C2 | `sessions/phase-c2-session-ui.md` | routing `/p/:projectId/s/:cid`, the conversation-keyed chat store, session header and list, new-session dialog (including gate warning and close/reopen buttons against the S-C4 contract), the "Plan open in Session N" banner, session chips in History and Changes, push deep link | C1, C3 (both wave 1) |
| C4 | `sessions/phase-c4-docs-panel.md` | the Docs panel (index, view, edit with conflict flow, approve, clear-user-edited, history diff, revert, live updates, phone) and the `doc-changed` notice card | C3 |
| C5 | `sessions/phase-c5-types-handover.md` | profiles, the `PlanGate` refusal naming the owning session, closing refused while the session owns an open plan, per-type prompts, the `sessions` skill key and per-type plugin dir, new skills, wiring the doc context (write policy, `post_doc_notice`), context injection, close/reopen and its lifecycle, gate notice, `FakeAgent` extensions, golden handover scenario, researcher query cap | C1, C3 |
| C6 | `sessions/phase-c6-eval-release.md` | Skills GUI `sessions` chips, live guard test and live eval, user docs and upgrade notes, as-built notes in this index, release v0.3.0 | C2, C4, C5 |

### Execution waves — what runs IN PARALLEL

A phase starts once every phase it depends on is merged. Phases in the same
wave run **concurrently in separate worktrees**.

| Wave | Phases (parallel) | Why they do not conflict |
|---|---|---|
| **1** | **C1 ∥ C3** | C1 owns `agent/engine.py`, `agent/runtime.py`, `db/repos/messages.py` and `agent_state.py`, `web/ws.py`, `web/routes_messages.py`, `agent/protocol.py`, the frontend chat store and interim UI, the `openPanel` store and its `ProjectLayout` wiring, and the history restore client. C3 owns the new package `design_docs/`, new routes `web/routes_docs.py`, `tools/spec.py` (new optional fields only), `agent/guards.py` (hook helper) and `agent/sdk_backend.py` (`build_options` hook wiring only), `tests/unit/phase8/test_builtin_skills.py` (EXPECTED plus the `doc` prefix), and the frontend `api/docs.ts` (`getDocIndex` only). Migration numbers are fixed upfront: C1 = `0008`, C3 = `0009`. `0009` does not depend on `0008`, because it has no foreign key to `conversations`. |
| **2** | **C2 ∥ C4 ∥ C5** | C2 is frontend only (plus the one-line push URL): router, the conversations store, chat components, `components/sessions/*`, History and Changes panels, and the plan banner. It does not touch `ProjectLayout.tsx`. C4 is frontend only: a new `panels/DocsPanel*` and `components/docs/*`, the `doc-changed` case in `MessageItem`, and a `wide` option on `PanelSheet`. C5 is backend plus skills: `agent/profiles.py`, `sdk_backend.py`, `engine.py` (injection and close hooks), `agent/runtime.py` (factory wiring), `plans/gate.py` (refusal text), `skills/`, `agent/fake.py`, `web/routes_conversations.py` (close/reopen). |
| **3** | **C6** | integration, eval and release |

**Known one-line overlap points.** Each side only adds lines. Rebase on
`main` before merging, and keep both sides:
- `tools/registry.py` `_PROVIDERS` (C3)
- `agent/sdk_backend.py`: C1 changes `start()` (signature, turn-context
  provider, `run_id` in the KiCad-less ctx). C3 touches **only**
  `build_options` (the hook), and nothing in `start()`.
- `agent/protocol.py`: C1 changes the frame fields, and C3 appends the
  `DocUpdatedFrame` class plus one union entry
- `web/app.py` routers and lifespan (C1, C3, C5)
- `protocol-fixtures/`, `protocol/frames.ts` `ALL_FRAME_TYPES` and the backend
  frame union (C1, C3)
- `panels/index.ts` (C4)
- the notice switch in `components/chat/MessageItem.tsx` (C2's
  `close-session` row, C4's `doc-changed` card)
- `ProjectLayout.tsx`: C1 wires the `openPanel` store (S-C8). In wave 2, C4
  only adds the `wide` prop. C2 does not touch it.
- `frontend/src/api/docs.ts`: C3 creates `getDocIndex` with the
  `DocIndexDTO` type. In wave 2, C4 adds the other calls, and C2 only
  imports `getDocIndex`.

Critical path: C1 → C2/C5 → C6. C3 and C4 have slack.

---

## Global Constraints

Every task implicitly includes these, plus the foundation and editing global
constraints.

- **The main agent has no file, shell or web tools.** The doc tools are
  CopperChat MCP tools. Built-ins stay `[]` or `["Skill"]`.
- **The doc path rules of spec §7.5 are binding for every reader and
  writer:** tools, REST, enumeration, index, change scan, revisions, gate
  **and project- or user-layer templates**.
  - **Writes are dir-fd relative.** `docs/design` is opened component-wise
    with `O_DIRECTORY|O_NOFOLLOW` (`dir_fd` chain). The temp file is
    created with `O_CREAT|O_EXCL|O_NOFOLLOW` relative to that fd, and moved
    with `os.replace(src_dir_fd=, dst_dir_fd=)`. This is a doc-only writer
    in `design_docs/paths.py`. The shared `fsutil.atomic_write_*` stays
    unchanged.
  - Lexical `lstat` checks.
  - Opens use `O_NOFOLLOW | O_NONBLOCK | O_NOCTTY`, followed by `fstat`
    (`S_ISREG`, size ≤ 256 KiB).
  - All file I/O runs in one `asyncio.to_thread`.
- **Frontmatter is server-owned.** Agent tools read and write bodies only.
  Approval is `doc_approvals` DB state. The frontmatter `status` only mirrors
  it.
- **Version** = `"v" + sha256(body bytes)[:16]`. Every conflict check hashes
  the actual bytes under the per-project doc lock.
- **One active run per project.** A burst never mixes conversations. Stop
  cancels only the stopped conversation's queue (§5.2).
- **Every `conversationId` from a client is ownership-checked** against the
  project: REST path, REST body (restore) and WS frames. On a mismatch,
  nothing is bound, enqueued or restored.
- **Injected doc text never reaches the system prompt or `TurnInfo.text`.**
  It is sanitised and nonce-fenced (§9.1).
- **No compatibility shims.** Frontend and backend ship together. Old tabs
  get the build-version reload banner.
- **Migrations:** `0008_conversations.sql` (C1) and `0009_design_docs.sql`
  (C3). No other phase adds a migration. Timestamps use `utcnow_iso()`
  format (`…Z`).
- **Never request Copilot.** Reviews use `kontor-recipes:review-loop`.

## Review Focus

These failure modes would hurt a user although no happy-path test hits them.
Each names the owning task.

1. **Queued messages land in the wrong session or overtake each other.** A1,
   B, A3 with both timings, a seal during wind-down, and Stop scoped to one
   conversation. Owner: C1 Task 3.
2. **Upgrade loses history or the resume id.** The migration fixture DB, and
   resuming the migrated session. Owners: C1 Task 1, C6 Task 1.
3. **A doc write escapes `docs/design/` or reads a symlink or FIFO**, for
   example a cloned repo's `requirements.md` → `/proc/self/environ`. Owner:
   C3 Task 1.
4. **A user edit is clobbered, or an approval is faked** by a stale
   `base_version`, a size/mtime-preserving external write, or a hand-written
   `status: approved`. Owner: C3 Tasks 3, 4.
5. **A profile leak.** A Requirements session reaches `sch_*` writes
   through the tool list, `can_use_tool` or the hook. Doc text poses as a
   `[CopperChat notice]`, or authorises `git_commit`. Owners: C5 Tasks 1, 4;
   C3 Task 6.
6. **A close-session stuck in `closing`** after a rate limit, cancel,
   `_fail_burst` or crash. Owner: C5 Task 5.

---

## Repository layout (additions)

```
backend/src/copperchat/
  db/migrations/0008_conversations.sql        C1
  db/migrations/0009_design_docs.sql          C3
  db/repos/conversations.py                   C1
  web/routes_conversations.py                 C1 (close/reopen: C5)
  design_docs/__init__.py                     C3  install_design_docs(app)
  design_docs/paths.py                        C3  resolve_doc_path, safe_read, walk
  design_docs/frontmatter.py                  C3
  design_docs/sections.py                     C3
  design_docs/service.py                      C3  DesignDocs
  design_docs/repo.py                         C3  revisions + approvals
  design_docs/specs.py                        C3  TOOL_SPECS (doc_*)
  design_docs/sanitize.py                     C3  notice/index sanitiser (used by C5)
  web/routes_docs.py                          C3
  agent/profiles.py                           C5
  agent/context_injection.py                  C5
  skills/builtin/design-docs/{SKILL.md,templates/*.md}   C3 (templates) / C5 (SKILL.md body)
  skills/builtin/session-requirements/SKILL.md           C5
  skills/builtin/session-schematic/SKILL.md              C5
frontend/src/
  stores/conversations.ts                     C2
  components/sessions/*                       C2
  api/conversations.ts                        C1 (interim) / C2
  api/docs.ts, stores/docs.ts                 C4
  panels/DocsPanel.tsx, panels/docs.ts        C4
  components/docs/*                           C4
  lib/buildVersion.ts + banner                C1
```

---

## Interface Contracts

These are the names and shapes that phases running in parallel rely on. A
phase that must deviate updates this section in its own PR, and adds an "as
built" bullet.

### S-C1. Database (C1: `0008`; C3: `0009`)

- **`0008_conversations.sql`** follows spec §4.2 exactly for `conversations`,
  the `conversation_id` columns on `messages`, `runs` and `plans`, the
  indexes, the backfill and `DROP TABLE agent_sessions`. It does **not**
  contain `doc_revisions` or `doc_approvals`.
- **`0009_design_docs.sql`** contains `doc_revisions` (with `added`,
  `removed`, `bytes`) and `doc_approvals`, as in spec §4.2.
  `doc_revisions.conversation_id` is plain TEXT with no FK, so `0009` is
  independent of `0008`.

### S-C2. `ConversationRepo` (C1) — `db/repos/conversations.py`

```python
@dataclass(frozen=True)
class ConversationRow:
    id: str; project_id: str; seq: int; title: str
    type: Literal["free", "requirements", "schematic", "board"]
    status: Literal["open", "closing", "closed"]; archived: bool
    sdk_session_id: str | None; docs_seen: dict[str, str]
    created_at: str; updated_at: str; last_message_at: str | None; closed_at: str | None

class ConversationRepo:
    async def create(self, project_id: str, type: str, title: str | None) -> ConversationRow  # seq retry on UNIQUE
    async def get(self, conversation_id: str) -> ConversationRow | None
    async def get_for_project(self, project_id: str, conversation_id: str) -> ConversationRow  # raises ConversationNotFound
    async def list(self, project_id: str, archived: bool = False) -> list[ConversationRow]
    async def update(self, cid: str, *, title: str | None = None, archived: bool | None = None) -> ConversationRow
    async def set_status(self, cid: str, status: str, *, expect: str) -> ConversationRow  # UPDATE … WHERE status=expect; 0 rows → ConversationStateConflict (409)
    async def set_sdk_session(self, cid: str, sdk_session_id: str) -> None
    async def set_docs_seen(self, cid: str, docs_seen: dict[str, str]) -> None
    async def touch(self, cid: str) -> None              # last_message_at, updated_at
    async def usage_totals(self, project_id: str) -> dict[str, dict]  # cid -> {inputTokens, outputTokens, costUsd}
    async def closing(self) -> list[ConversationRow]     # all projects (C5 startup sweep)
```

`ConversationNotFound` (`code="conversation_not_found"`, HTTP 404) lives in
`copperchat.errors`.

**As built (C1, PR #43):**
- Extra helpers: `ConversationRepo.for_runs`, `ConversationRepo.usage_for`,
  `ConversationRow.to_wire`.
- Errors: `ConversationClosed` (409), `ConversationStateConflict` (409),
  `InvalidConversation` (422).

### S-C3. Engine (C1)

- `QueuedItem` gains `conversation_id: str`. It is a required field, placed
  after `client_message_id`.
- `RunEngine.submit_ex(conversation_id, client_message_id, text, attachments,
  metadata=None)`. It raises `ConversationClosed`
  (`code="conversation_closed"`) unless the conversation is `open`.
  - Exception: `metadata.origin == "close-session"` may be submitted while
    the conversation is `closing`.
- `RunEngine.post_notice(conversation_id, text, metadata=None)` and
  `post_error(conversation_id, …)`.
- `RunEngine.add_agent_note(text, conversation_id: str | None = None)`.
  `None` means a broadcast to every open conversation, delivered at its next
  turn.
- `RunEngine.interrupt(run_id, conversation_id)`, scoped as in spec §5.2.
- `RunEngine.current_conversation_id -> str | None`.
- **Burst hook for C5.** `AgentBackend.start(project, conversation:
  ConversationRow, input_stream, resume_session_id)`. C1 passes the row, and
  the SDK backend ignores the type until C5.
- **Turn hook for C5.** `RunHooks.on_turn_start(turn)`. `TurnInfo` gains
  `conversation_id`, plus a new hook `async def
  RunHooks.before_model_input(turn) -> list[str]`, which returns extra
  notices. **Each list entry is one notice.**
  It may contain newlines, and it gets **exactly one** `[CopperChat notice]`
  prefix, at its start. Lines inside an entry are never prefixed. This
  matters because the index rows and the fenced doc body are repository
  data (spec §9.1). C1 adds the hook with a
  no-op default, and calls it in `_begin_turn` next to the notes prefix. C5
  implements context injection through it.
- **`ChainedHooks` forwards the new methods (C1).** In production the
  engine only ever sees `ChainedHooks` (`install_editing` wraps history,
  `editing/__init__.py:50`; `ChainedHooks` is at `agent/engine.py:124-153`).
  C1 extends it with a test:
  - `before_model_input` concatenates the lists of all hooks, in order.
  - `on_items_failed` runs all hooks, then raises the first error.
  - `before_model_input` stops at the first error. `_begin_turn` catches
    and logs it, like `on_turn_start` (`engine.py:576-579`), and the turn
    continues without the injected notices. C1 test: a raising hook does
    not fail the turn.

  C5 installs `SessionHooks` by wrapping `runtime.hooks_factory`, as
  `install_editing` does, in `install_sessions(app)` after
  `install_plans`. It tests through the app's real `hooks_factory`, not by
  passing hooks directly.
- **Turn context for tools (C1).**
  - **Conversation fields are burst-scoped.** `start(project,
    conversation, …)` binds them, because the conversation is fixed per
    burst. So a turn the CLI starts on its own (`ev.unsolicited`,
    `engine.py:591`, where `_turn` is None) still gets its session's
    profile and policy.
  - **`run_id` comes from the engine.** `turn_context() -> run_id | None`.
    `AgentRuntime` passes it through `BackendFactory` into the SDK backend
    and the `FakeAgent`.
  - **C1's scope.** C1 sets `run_id` in the SDK backend's KiCad-less ctx
    factory. It also gives `FakeAgent` the KiCad-less path:
    `build_sdk_tool_list(specs, ctx_factory, kicadless_ctx_factory)`, as
    the SDK backend does (`agent/fake.py:196`).
  - **C5's scope.** C5 adds the conversation fields to both factories.
  - **Mutating doc tools refuse when `run_id` is None**
    (`doc_scope_refused`, "no active turn"), so the per-turn cap cannot
    collapse into one `(pid, None)` bucket.
- **Turn-end hook for C5.** `TurnOutcome` gains `conversation_id` and the
  final message status. C5's close lifecycle uses `on_turn_end`, plus a new
  `RunHooks.on_items_failed(conversation_id, message_ids, status)` that C1
  calls from `_fail_burst`, `cancel_queued` and `interrupt` cancellation.

**As built (C1, PR #43):**
- `RunEngine.interrupt(run_id, conversation_id=None)`.
- `replay_and_subscribe(cid, since)` returns `(dtos, sub, run_id,
  run_conversation_id)`.
- `TurnOutcome.conversation_id` and `message_status` have defaults (`None` and
  `"done"`).
- Extra helpers: `engine.conversation_dto` / `publish_conversation`,
  `PlanRepo.run_conversation`. `Plan.conversation_id` and
  `new_plan(conversation_id=)` are required, and
  `restore_snapshot(..., conversation_id=)` takes the conversation.

**As built (C5):**
- `RunEngine.turn_context()` returns the **KiCad-less `ToolContext`** (no
  `session`; run id, user text, the burst's conversation fields, `docs`,
  `post_doc_notice`), not the bare run id. `BackendFactory` is
  `(ctx_factory, turn_context: () -> ToolContext)`; `_make_ctx` is
  `replace(turn_context(), session=…, callbacks…)`. Both backends also bind the
  burst's conversation (`agent.profiles.bind_conversation`).
- `install_sessions(app)` flattens the chain: `engine.hooks` is one
  `ChainedHooks([history, editing, SessionHooks])`. The service is
  `app.state.conversation_service` (`app.state.sessions` stays the KiCad session
  manager). `sweep_closing(db)` runs in the lifespan after `pause_all_running`,
  before `resume_pending`.
- The first-turn marker is the `docs_seen` key `#indexed`; a conversation without it
  gets the index at its next turn (also migrated ones).

### S-C4. REST and WS (C1; close/reopen: C5; docs: C3)

- **REST** follows spec §6.1. `ConversationDTO` (camelCase):

  ```
  {id, seq, title, type, status, archived, createdAt, lastMessageAt,
   closedAt, usage}
  ```

- **C1 routes:** `GET/POST /conversations`, `PATCH /conversations/{cid}`,
  `GET /conversations/{cid}/messages`. The old `GET /messages` is removed.
- **C5 routes:** `POST /conversations/{cid}/close` → 202 `ConversationDTO`
  (`closing`), and `POST /conversations/{cid}/reopen`. C2 builds its UI
  against these shapes.
- **WS** follows spec §6.2.
  - Frames gain `conversationId` where listed.
  - New frames: `conversation-updated` (C1) and `doc-updated` (C3).
  - `ALL_FRAME_TYPES` order: append-only.

**As built (C1, PR #43):**
- `POST /conversations` with `type='board'` → 400 `session_type_unavailable`.
- `conversation-updated` at turn end is published just before `run-finished`.

**As built (C3, PR #42):**
- `GET /docs/{path}` `meta` = `{status, userEdited, approved, approvedVersion,
  approvedAt, changedSinceApproval}`.
- Doc error bodies: `{"error": {code, message, details}}`, with camelCase
  details (`currentVersion`, `author`, `conversationTitle`, `diff` for
  `doc_conflict`).
- `doc-updated.revisionId` is `null` for approve, unapprove and
  clear-user-edited (no revision); `conversationId` uses the 32-hex
  `ConversationId` type.
- Revision routes use `{rid:int}`; a `rid` outside SQLite's integer range is a
  404.

### S-C5. `ToolContext` additions (C3)

```python
conversation_id: str | None = None
conversation_type: str | None = None   # None = treat as "free" ONLY while conversation_id is None too (plain tests)
conversation_seq: int | None = None
conversation_title: str | None = None
post_doc_notice: Callable[[DocNotice], Awaitable[None]] | None = None
docs: DesignDocs | None = None
```

- C3 uses a local `ConversationInfo(id, seq, title, type)` dataclass, built
  from these fields, for the decisions entry and attribution. It never
  imports C1's `ConversationRow`.
- **Fail closed (C3 implements and tests both cases).**
  - `(conversation_id None, conversation_type None)` → the Free policy.
    This is for plain tests only.
  - `(conversation_id set, conversation_type None)` → `doc_scope_refused`.
- **`DocNotice`** is a frozen dataclass:

  ```
  DocNotice(kind: 'changed' | 'approval_lost', path, revision_id, added,
            removed, sections: tuple[str, ...], conversation_id, run_id)
  ```

  `path` is always the full `docs/design/...` form, also in DTOs, frames
  and notice metadata. C5 maps `kind='changed'` to the S-C8 `doc-changed`
  metadata, with exactly the keys `action, path, revisionId, added,
  removed, sections`.
- `run_id` in the KiCad-less ctx is C1's job (S-C3 turn context).
- C5 wires all of them. C1 does **not** touch `ToolContext`. C5 fills it in
  `engine._make_ctx` and the SDK backend's KiCad-less factory.

**As built (C3, PR #42):**
- The doc tools refuse ("Design docs are not available") while `ctx.docs` is
  None; C5 wires `docs` into both context factories.

**As built (C5):**
- `DocNotice` gains `version: str | None = None` (the body version after the
  change): the engine puts it into the conversation's `docs_seen` at once.
- `kind='approval_lost'` becomes a plain notice "<doc> changed after approval
  (<session>) — review and re-approve" (metadata `{path}`).

### S-C6. `DesignDocs` service (C3) — `design_docs/service.py`

```python
class DesignDocs:
    async def list(self, project) -> DocIndex                 # incl. ignored entries; DocIndexDTO below
    async def read(self, project, path, section=None) -> DocRead
    async def write(self, project, path, body, base_version, *, author, summary,
                    conversation_id=None, run_id=None, policy: WritePolicy) -> DocWriteResult
    async def patch_section(self, project, path, heading, body, base_version, *, mode, create, ...) -> DocWriteResult
    async def append(self, project, path, text, *, section=None, title=None, base_version=None, ...) -> DocWriteResult
    async def append_server_entry(self, project, title, text, *, conversation: ConversationInfo) -> DocWriteResult | None  # §10, escape+truncate
    async def approve(self, project, path, version) -> str
    async def unapprove(self, project, path, version) -> str
    async def clear_user_edited(self, project, path, version) -> str
    async def revisions(self, project, path, limit=50) -> list[RevisionDTO]
    async def revision(self, project, path, rid) -> RevisionContent
    async def revert(self, project, path, rid, base_version) -> str
    async def versions(self, project) -> dict[str, str]       # cached by (mtime_ns,size): notices only
    def lock(self, project_id) -> asyncio.Lock
```

- **`WritePolicy`** is derived from the conversation type, via
  `policy_for(type)` in C3: Free, Requirements and Schematic are as in spec
  §8.2, and Board is read and append only. `doc-updated` is published by the
  service through the broadcaster.
- **`DocIndexDTO`** is the REST `GET /docs` shape and the `doc_list` shape:

  ```
  {root, docs: [{path, fixed, exists, approved, changedSinceApproval,
                 approvedVersion|null, approvedAt|null, userEdited,
                 version|null, bytes, updatedAt|null, headings,
                 openPoints, lastAuthor|null,
                 lastConversationTitle|null}],
   ignored: [{path, reason}]}
  ```

  The tool returns the same data in snake_case.
- **Session label.** `0009`'s `doc_revisions` gains
  `conversation_label TEXT NULL` (for example `"Session 2 — Schematic"`),
  written at write time. The revisions REST and `lastConversationTitle` use
  it, so no join with C1's table is needed.
- **`design_docs/sanitize.py`** offers `sanitize_inline(text)` and
  `sanitize_field(text, limit=80)`. C5 uses them for injection and C3 for
  `doc_list` fields.
- **Error codes:** `doc_path_refused`, `doc_conflict`, `doc_user_edited`,
  `doc_append_only`, `doc_scope_refused`, `doc_heading_refused`,
  `doc_frontmatter_refused`, `doc_too_large`, `doc_read_too_large`,
  `doc_section_not_found`, `doc_section_ambiguous`, `doc_limit`.

**As built (C3, PR #42):**
- `write`, `patch_section` and `append` take `conversation: ConversationInfo |
  None` instead of `conversation_id` (the revision label and the decisions
  entry need seq, title and type). `ConversationInfo.label` is
  `"<title> — <Type>"`.
- `fsutil.py` is unchanged: the doc-only dir-fd writer lives in
  `design_docs/paths.py` (Global Constraints).
- Extra codes: `doc_not_found` (404, unknown or foreign `rid`) and
  `invalid_input` (400). Non-UTF-8 docs are refused and listed as ignored
  (`not utf-8`).
- Policies: Schematic sessions never create a new doc (`decisions.md`
  excepted); Board may only `doc_append` to `decisions.md` and `board.md`; Free
  may write `board.md` (§8.2 table).
- On a user-edited doc, replacing a level-1 section or any section that spans
  the whole doc is refused with `doc_user_edited`.
- Only successful changes count toward the 40 doc mutations per turn.
- Line counts: exact `difflib` up to 200 lines, a linear multiset count above.
  The conflict diff keeps the 4,000-line cutoff.
- Residuals: cross-script look-alikes of `[CopperChat notice]` and
  `*Session:` lines; revisions of deleted docs are not pruned.

**As built (C5):**
- `DesignDocs` counts conflicts per `(project, run, path)`; from the third one in a
  turn the `doc_conflict` suggestion says to stop and ask the user.

### S-C7. Profiles (C5) — `agent/profiles.py`

- `PROFILES: dict[str, frozenset[str]]`, exactly as in spec §8.2.
  `profile_specs(type, specs)` filters the registry.
- `tests/unit/agent/test_profiles.py` asserts two things:
  - Free ⊇ registry.
  - Each name exists in the registry.
- The `sessions` front-matter key is parsed in `skills/store.py`
  (`EffectiveSkill.sessions: frozenset[str] | None`).

**As built (C5):**
- `profile_specs("free", specs)` returns every spec (new tools reach Free
  automatically); an unknown type raises. `PROFILES["free"]` lists today's 45.
- Board (hidden) = the 13 KiCad read tools, library, parts, git, `doc_list`,
  `doc_read`, `doc_append`.
- `assemble_skills(settings, project, db, conversation_type="free")`; the skills
  DTO gains `sessions: string[] | null` (sorted).
- The skill tool-name test checks the creatable types (free, requirements,
  schematic); Board's profile is SP4's. So `design-docs` (all types) names only
  `doc_list`, `doc_read`, `doc_patch_section`, `doc_append`; `doc_write` is
  described in `session-requirements`.
- `PlanGate` refusals of another session's open plan name its title (`what`,
  `suggestion`, `details.owner`); `PlanRepo.open_owner(pid)` gives
  `(plan id, status, conversation id)`.
- `plan_step_begin`, `plan_step_done` and `plan_revise` from a session that does not
  own the open plan are refused (`plan_open`, naming the owner), so no session takes
  over another's plan.
- A project-layer copy of a skill cannot drop the `sessions` restriction of its
  built-in/user version (both must allow the type).

**As built (C6):**
- The Skills page shows `sessions` as type chips on every skill. User skills edit them
  through the existing editor and `PUT /skills/user/{name}`: the panel writes the flat
  front-matter line `sessions: requirements, schematic` (sorted; no chip = key removed =
  all types), the format `parse_sessions` reads. Project-layer skills show read-only
  chips: there is no project-skill write route (they are edited in
  `.kicad-agent/skills/`), so the "editable for project layers" part of §8.4 is a residual.

### S-C8. Frontend contracts

- **C1 `openPanel`.** `stores/ui.ts` gains `openPanel(id: string, params?:
  Record<string,string>)` and `panelRequest`. `ProjectLayout` (desktop
  sheet and phone tab) opens the requested panel and passes `params` as an
  optional `PanelProps.params`. C2 uses it for [Open requirements]. C4 uses
  it for [View diff] (`{revisionId}`) and for relative links to Parts.
- **C3 `api/docs.ts`.** `getDocIndex(pid): Promise<DocIndexDTO>` plus the
  zod schema. C4 extends the file.
- **C1 (interim).** `stores/chat.ts` is keyed `` `${projectId}/${conversationId}` ``.
  `useChat(projectId, conversationId)`. `api/conversations.ts` provides
  `listConversations`, `createConversation`, `patchConversation`, and the
  C5 routes `closeConversation` and `reopenConversation`.
- **C2.** `stores/conversations.ts` holds `byProject: Record<pid,
  ConversationDTO[]>` and `activeRun: Record<pid, {conversationId, runId} |
  null>`. Route `/p/:projectId/s/:cid`.
- **C4.** `stores/docs.ts` and `api/docs.ts`. The `doc-changed` notice
  metadata is `{action:'doc-changed', path, revisionId, added, removed,
  sections: string[]}`.

**As built (C4):**
- `Markdown`'s `onRelativeLink?: (href) => (() => void) | null` receives every
  link that is not http(s)/mailto; the returned action renders a button, `null`
  plain text. Without it (chat), links behave as before.
- `openPanel('docs', {path, revisionId})`: `path` is the full `docs/design/…`
  path, `revisionId` a decimal string. `openPanel('parts', {mpn})` opens a part.
- `PanelDefinition.wide` → the desktop/tablet sheet is `max-w-3xl`.
- `stores/docs.ts` keeps `live[path]` (the last `doc-updated` per path); an
  editor compares it with its base version for the "your save will conflict"
  banner. Index and doc refreshes are coalesced (≤ 2 requests per burst).
- A revert that hits a 409 continues in the editor's conflict flow, seeded with
  the revision's body.
- The diff (`components/docs/lineDiff.ts`) keeps one `Int32Array(d+1)` per step
  (≤ (D+1)(D+2)/2 entries); revision texts are shown without front matter.

**As built (C2):**
- `stores/conversations.ts` also holds `status`, `errors`, `unread` and
  `viewing` per project, plus the actions `load`, `loadArchived`, `view`,
  `create`, `rename`, `setArchived`, `close`, `reopen` and `interrupt`
  (sends `interrupt` with `activeRun.conversationId`). `byProject` includes
  archived sessions once listed. Hooks: `useConversations`,
  `useConversation`, `useConversationsApi`; tests inject a store through
  `ConversationsStoreContext`. The chat store keeps its own `activeRuns`.
- The composer keeps one draft per conversation: `useComposer.activate(chatKey)`
  (called by `ChatPanel`, which is keyed by the session); `deactivate()` while no
  chat is shown; `reset()` drops all drafts on logout.
- The composer attachment slot (`composerSlots.ts`, Phase 8 extension point)
  takes `addAttachment(ref)` instead of `setAttachments(list)`: an upload adds
  to the draft it started in (`addAttachmentFor(key, ref)`), also after a switch.
- `ChatDeps.onResubscribe(projectId)` tells the conversations store about a
  session switch (`resubscribed`): it forgets the active run and re-reads the
  list; a reconnect re-reads only projects whose page is shown (`leave()`).
- `ProjectPage` loads the open plan (plans store) for the "Plan open in
  Session N" banner; `viewer.spec.ts` mocks `GET /plans/open`.
- Run pushes deep-link to `/p/{pid}/s/{cid}`; the rate-limit push keeps
  `/p/{pid}`. `public/sw.js` `safeUrl` accepts the session path.

---

## Decided by owner (2026-10-19)

All 14 recommendations of the spec's §17 were accepted:
1A 2A 3A 4B 5B 6A 7A 8A 9A 10B 11A 12A 13A 14A. The spec body is binding as
written.

## Calls made by the plan author

- **Migration split.** `0009` (docs) is separate from `0008` so that C1 and
  C3 can run in parallel. The spec's §4.2 code block shows both in one file,
  but only the split is normative.
- **The close/reopen UI lives in C2**, built against the S-C4 contract. The
  backend is in C5. The UI's e2e test runs in C6.
- **C1 adds the engine hooks** (`before_model_input`, `on_items_failed`,
  `turn_context`) that C5 needs, so that C5 does not reshape the engine
  again.
- **Closing a session that owns an open plan** (proposed, approved, running
  or paused) is refused with 409 `plan_open`, "finish or abort the plan
  first". Otherwise Approve or Resume would target a closed session while
  the plan keeps blocking edits (C5).
- **The build version comes from `frontend/package.json`.** The Docker
  frontend stage has no backend files. `scripts/check-docs.sh` keeps it
  equal to `pyproject` and `__version__`.
- **Skills GUI `sessions` chips go to C6.** The frontend reads the field
  that C5 adds to the skills DTO.
- **Plan review amendments** (round 1, 22 findings) are folded into the
  phase files.

---

## As built and residuals (SP3, after C6)

All six phases are merged as one PR each: C1 #43, C3 #42, C2 #45, C4 #44, C5 #46, C6 (this
PR). The "As built" bullets under the contracts above record the deviations.

**Live checks (2026-10-20, model: CLI default, cost = SDK list-price estimate on the OAuth
subscription; results in `backend/tests/live/results/2026-10-20.json`):**
- **Upgrade** (`scripts/live-upgrade-check.sh`): `ghcr.io/peterus/copperchat:0.2.0` chats once
  on a fresh data dir (a code word); this checkout, in the kicad-base image with the same
  `/projects` and `/data`, migrates it, opens "Session 1" (Free, history kept) and the resumed
  SDK session answers the code word. ✅
- **Guard** (`test_sessions_guard_live.py`): a Requirements session (a) has no `sch_connect`
  (no `sch_*` call), (b) cannot read `/etc/passwd` (no tool tried, nothing leaked), (c)
  recognised the injected `requirements.md` as an exfiltration attempt and made no
  `research_part` call (so no researcher fetch with doc content). US$0.22. ✅ In this run
  (b) and (c) held because the model refused; the server-side guards behind them (path
  guard, query cap, profile filter, hook) are covered by the C3/C5 unit tests, not by this
  run.
- **Eval** (`test_sessions_live.py`, run in the kicad-base image because it needs KiCad 11):

  | Session | Turns | Plan | Result | Output tokens | US$ | s |
  |---|---|---|---|---|---|---|
  | Requirements (+ close) | 1 + 1 | – | 4/4 sections, ATtiny85 + USB-C researched (2 `research_part`), 5 decisions, 0 `sch_*` | 25.5 k | 0.42 | 181 |
  | Schematic (from the approved docs) | 2 | 4 steps | golden ✅, ERC 0/0 | 16.9 k | 0.77 | 186 |
  | **Split total** | | | | 42.4 k | **1.19** | 367 |
  | Baseline: one Free session | 2 | 3 steps | golden ✅, ERC 0/0 | 26.0 k | **0.84** | 257 |

  The split costs about 40 % more than one session for this small board: the requirements
  session researches parts and writes four docs that the baseline skips. The schematic
  session itself was cheaper than the baseline. A first eval run was void (harness bug: the
  project was not opened in KiCad, fixed by `LiveApp.open_project`). Skills were not changed.

**Residuals (deferred, all low):**
- C1: the upgrade check on a copy of the real prod database is left to the owner:
  `COPPERCHAT_UPGRADE_DB=<copy of copperchat.db> uv run pytest tests/unit/db/test_upgrade_v020.py`
  (back up `data/` before the prod upgrade).
- C3 (pm#6): revisions of deleted docs are not pruned.
- C3: cross-script look-alikes of `[CopperChat notice]` and `*Session:` lines are not
  normalised.
- C5: a close that is reverted (Stop, error) can race with the re-drive of a parked close
  message.
- C5: the `plan_propose` refusal hint mentions `plan_revise` also to sessions that do not
  own the open plan (which then get `plan_open`).
- §13: the researcher's `WebFetch` may reach any public host (no datasheet allow-list);
  mitigated by the 2,000-char query cap and the researcher prompt.
- §7.2: templates are not editable in the Skills GUI (files in
  `<data>/skills/design-docs/templates/` or the project layer).
- C6: project-layer skills' `sessions` chips are read-only (see S-C7 as built).
- Eval observation: in the first turn of the Requirements session the CLI answered four
  calls (`parts_get_notes`, `doc_read`) with "No such tool available"; the model retried
  them at once and they worked. Not reproduced in the guard run; probably the CLI's MCP
  tool list was not ready yet. Watch for it in prod.
- Board sessions (type `board`, profile defined) stay hidden until SP4.

---

## Phase Conventions (apply to every phase)

- **Branch/PR:** one PR per phase, titled `sessions phase <C#>: <short goal>`,
  created with the `gh` CLI. Commits end with `Co-Authored-By: Claude Opus 5.5
  <noreply@anthropic.com>`. PR bodies end with `🤖 Generated with [Claude
  Code](https://claude.com/claude-code)`.
- **TDD:** failing test → see it fail → minimal code → pass → commit, per
  task.
- **Backend commands:** `cd backend && uv run pytest -m "not kicad" -q`,
  `uv run ruff check . && uv run ruff format --check . && uv run mypy`.
- **Frontend commands:** `cd frontend && npm run typecheck && npm run lint &&
  npm test -- --run && npm run build && npx playwright test`. CI runs all
  Playwright specs, and existing specs mock API routes that C1 and C2
  change.
- **KiCad:** no phase in this plan needs real KiCad tests, except where a
  phase says so. The doc tools are KiCad-less.
- **Protocol fixtures** must round-trip canonically (backend
  `test_protocol.py`). Update both sides in the same PR.
- **Review:** after CI is green, run `kontor-recipes:review-loop <PR>
  --max-rounds 2 --dimensions bugs+tests,security+performance`, with a fresh
  reviewer child per round. If round 2 still fixed code, run one
  `--review-only` verify pass. **Never Copilot.**
- **Done report:** `kontor_send_to_session(<coordinator>, status:'done')`
  with PR, CI, review result and deviations. Contract deviations go into
  this index, in the same PR, as an "as built" bullet under the contract.
- **No placeholders** in merged code. Stubs are allowed only where this plan
  names a later owner (for example, C1's no-op `before_model_input`).
