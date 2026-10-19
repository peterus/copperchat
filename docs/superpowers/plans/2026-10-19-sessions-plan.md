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
| C2 | `sessions/phase-c2-session-ui.md` | routing `/p/:projectId/s/:cid`, the conversation-keyed chat store, session header and list, new-session dialog (including gate warning and close/reopen buttons against the S-C4 contract), session chips in History and Changes, push deep link | C1 |
| C4 | `sessions/phase-c4-docs-panel.md` | the Docs panel (index, view, edit with conflict flow, approve, clear-user-edited, history diff, revert, live updates, phone) and the `doc-changed` notice card | C3 |
| C5 | `sessions/phase-c5-types-handover.md` | profiles, per-type prompts, the `sessions` skill key and per-type plugin dir, new skills, wiring the doc context (write policy, `post_doc_notice`), context injection, close/reopen and its lifecycle, gate notice, `FakeAgent` extensions, golden handover scenario, researcher query cap | C1, C3 |
| C6 | `sessions/phase-c6-eval-release.md` | live guard test and live eval, user docs and upgrade notes, as-built notes in this index, release v0.3.0 | C2, C4, C5 |

### Execution waves — what runs IN PARALLEL

A phase starts once every phase it depends on is merged. Phases in the same
wave run **concurrently in separate worktrees**.

| Wave | Phases (parallel) | Why they do not conflict |
|---|---|---|
| **1** | **C1 ∥ C3** | C1 owns `agent/engine.py`, `agent/runtime.py`, `db/repos/messages.py` and `agent_state.py`, `web/ws.py`, `web/routes_messages.py`, `agent/protocol.py`, and the frontend chat store and interim UI. C3 owns the new package `design_docs/`, new routes `web/routes_docs.py`, `tools/spec.py` (new optional fields only), `agent/guards.py` (hook helper) and `agent/sdk_backend.py` (hook wiring only). Migration numbers are fixed upfront: C1 = `0008`, C3 = `0009`. `0009` does not depend on `0008`, because it has no foreign key to `conversations`. |
| **2** | **C2 ∥ C4 ∥ C5** | C2 is frontend only: router, stores, chat components, layouts, History and Changes panels. C4 is frontend only: a new `panels/DocsPanel*` and `components/docs/*`, the `doc-changed` case in `MessageItem`, and a `wide` option on `PanelSheet`. C5 is backend plus skills: `agent/profiles.py`, `sdk_backend.py`, `engine.py` (injection and close hooks), `skills/`, `agent/fake.py`, `web/routes_conversations.py` (close/reopen). |
| **3** | **C6** | integration, eval and release |

**Known one-line overlap points.** Each side only adds lines. Rebase on
`main` before merging, and keep both sides:
- `tools/registry.py` `_PROVIDERS` (C3)
- `agent/sdk_backend.py`: C1 changes the `start` signature, and C3 adds the
  hook in `build_options`, in different functions
- `agent/protocol.py`: C1 changes the frame fields, and C3 appends the
  `DocUpdatedFrame` class plus one union entry
- `web/app.py` routers and lifespan (C1, C3, C5)
- `protocol-fixtures/`, `protocol/frames.ts` `ALL_FRAME_TYPES` and the backend
  frame union (C1, C3)
- `panels/index.ts` (C4)
- the notice switch in `components/chat/MessageItem.tsx` (C2's
  `close-session` row, C4's `doc-changed` card)
- `ProjectLayout.tsx` (C2's header changes; C4 only adds the `wide` prop)

Critical path: C1 → C2/C5 → C6. C3 and C4 have slack.

---

## Global Constraints

Every task implicitly includes these, plus the foundation and editing global
constraints.

- **The main agent has no file, shell or web tools.** The doc tools are
  CopperChat MCP tools. Built-ins stay `[]` or `["Skill"]`.
- **The doc path rules of spec §7.5 are binding for every reader and
  writer:** tools, REST, enumeration, index, change scan, revisions and gate.
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
    async def set_status(self, cid: str, status: str) -> ConversationRow
    async def set_sdk_session(self, cid: str, sdk_session_id: str) -> None
    async def set_docs_seen(self, cid: str, docs_seen: dict[str, str]) -> None
    async def touch(self, cid: str) -> None              # last_message_at, updated_at
    async def usage_totals(self, project_id: str) -> dict[str, dict]  # cid -> {inputTokens, outputTokens, costUsd}
    async def closing(self) -> list[ConversationRow]     # all projects (C5 startup sweep)
```

`ConversationNotFound` (`code="conversation_not_found"`, HTTP 404) lives in
`copperchat.errors`.

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
  `conversation_id`, plus a new hook `RunHooks.before_model_input(turn) ->
  list[str]`, which returns extra notice lines. C1 adds the hook with a
  no-op default, and calls it in `_begin_turn` next to the notes prefix. C5
  implements context injection through it.
- **Turn-end hook for C5.** `TurnOutcome` gains `conversation_id` and the
  final message status. C5's close lifecycle uses `on_turn_end`, plus a new
  `RunHooks.on_items_failed(conversation_id, message_ids, status)` that C1
  calls from `_fail_burst`, `cancel_queued` and `interrupt` cancellation.

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

### S-C5. `ToolContext` additions (C3)

```python
conversation_id: str | None = None
conversation_type: str | None = None   # None = treat as "free" (tests, fake agent before C5)
post_doc_notice: Callable[[DocNotice], Awaitable[None]] | None = None
docs: DesignDocs | None = None
```

- C3 also sets `run_id` in the backend's KiCad-less context factory.
- C5 wires all of them. C1 does **not** touch `ToolContext`. C5 fills it in
  `engine._make_ctx` and the SDK backend's KiCad-less factory.

### S-C6. `DesignDocs` service (C3) — `design_docs/service.py`

```python
class DesignDocs:
    async def list(self, project) -> DocIndex                 # incl. ignored entries
    async def read(self, project, path, section=None) -> DocRead
    async def write(self, project, path, body, base_version, *, author, summary,
                    conversation_id=None, run_id=None, policy: WritePolicy) -> DocWriteResult
    async def patch_section(self, project, path, heading, body, base_version, *, mode, create, ...) -> DocWriteResult
    async def append(self, project, path, text, *, section=None, title=None, base_version=None, ...) -> DocWriteResult
    async def append_server_entry(self, project, title, text, *, conversation) -> DocWriteResult | None  # §10, escape+truncate
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
- **`design_docs/sanitize.py`** offers `sanitize_inline(text)` and
  `sanitize_field(text, limit=80)`. C5 uses them for injection and C3 for
  `doc_list` fields.
- **Error codes:** `doc_path_refused`, `doc_conflict`, `doc_user_edited`,
  `doc_append_only`, `doc_scope_refused`, `doc_heading_refused`,
  `doc_frontmatter_refused`, `doc_too_large`, `doc_read_too_large`,
  `doc_section_not_found`, `doc_section_ambiguous`, `doc_limit`.

### S-C7. Profiles (C5) — `agent/profiles.py`

- `PROFILES: dict[str, frozenset[str]]`, exactly as in spec §8.2.
  `profile_specs(type, specs)` filters the registry.
- `tests/unit/agent/test_profiles.py` asserts two things:
  - Free ⊇ registry.
  - Each name exists in the registry.
- The `sessions` front-matter key is parsed in `skills/store.py`
  (`EffectiveSkill.sessions: frozenset[str] | None`).

### S-C8. Frontend contracts

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
- **C1 adds the engine hooks** (`before_model_input`, `on_items_failed`) that
  C5 needs, so that C5 does not reshape the engine again.

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
  npm test -- --run && npm run build`.
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
