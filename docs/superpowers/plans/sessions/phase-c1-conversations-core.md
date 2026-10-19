# Phase C1: Conversations core (DB, engine, REST/WS, interim UI)

> **Index contracts:** S-C1 (`0008`), S-C2, S-C3, S-C4 (C1 part), S-C8 (C1
> part).
> **Spec sections:** §4, §5, §6.1 (conversations, messages, restore), §6.2,
> §6.3.
> **Runs in parallel with C3.** Do not touch `design_docs/`,
> `web/routes_docs.py` or `tools/spec.py`.

**Goal:** Messages, runs, plans and the SDK session belong to a
conversation. The engine runs one burst per conversation, with FIFO across
conversations and a scoped Stop. REST and WS speak `conversationId`. The
existing UI keeps working through an interim "default session" shim, and
old tabs see a reload banner.

**Files:**
- Create:
  - `db/migrations/0008_conversations.sql`
  - `db/repos/conversations.py`
  - `web/routes_conversations.py`
  - `frontend/src/api/conversations.ts`
  - `frontend/src/lib/buildVersion.ts`
  - `frontend/src/components/UpdateBanner.tsx`
- Modify:
  - Agent: `agent/events.py` (`QueuedItem.conversation_id`),
    `agent/push_stream.py` (`conversation_id`, `seal()`), `agent/engine.py`,
    `agent/runtime.py` (`BackendFactory` gets `turn_context`),
    `agent/sdk_backend.py` (`start()`: signature, turn context, `run_id` in
    the KiCad-less ctx; never `build_options`),
    `agent/fake.py` (the `start` signature; the KiCad-less ctx path via
    `build_sdk_tool_list(specs, ctx_factory, kicadless_ctx_factory)`),
    `agent/protocol.py`
  - Hooks: `ChainedHooks` in `agent/engine.py` forwards
    `before_model_input` and `on_items_failed` (S-C3)
  - Existing e2e: `frontend/e2e/smoke.spec.ts` and `frontend/e2e/viewer.spec.ts`
    (mock `/conversations*` and the new messages route; `conversationId` in
    frames)
  - Repos and routes: `db/repos/messages.py`, `db/repos/agent_state.py`
    (remove `AgentSessionRepo`, `RunRepo.start(run_id, project_id,
    conversation_id)`), `web/ws.py`, `web/routes_messages.py` (remove the
    page route), `web/routes_history.py` (restore body, DTO fields),
    `web/routes_editing.py` (`/changes/runs` DTO fields), `web/app.py`
  - Callers of notices and notes: `plans/service.py` (notices into
    `plans.conversation_id`, set on propose), `history/restore.py`,
    `history/hooks.py`, `editing/hooks.py`
  - Push: `push/notifier.py` (session title in the push text; the URL stays
    `/p/{pid}` until C2)
  - `protocol-fixtures/*.json`, `protocol-fixtures/conversation-updated.json`
  - Frontend: `frontend/src/protocol/frames.ts`, `frontend/src/stores/chat.ts`,
    `frontend/src/hooks/useChat.ts`, `frontend/src/components/chat/ChatPanel.tsx`,
    `frontend/src/pages/ProjectPage.tsx`, `frontend/src/components/plan/PlanCard.tsx`,
    `frontend/src/panels/ChangesPanel.tsx`,
    `frontend/src/components/history/{restore.ts,ConfirmRestore.tsx,RestoreOffer.tsx}`
    (restore sends `conversationId`), `frontend/src/test/fakeMessageServer.ts`
    (it mirrors the new messages route), `frontend/src/stores/ui.ts` and
    `frontend/src/layouts/{ProjectLayout.tsx,panels.ts}` (`openPanel`, S-C8)
- Tests:
  - `tests/unit/db/test_migration_0008.py`
  - `tests/unit/db/test_conversations_repo.py`
  - `tests/unit/agent/test_engine_conversations.py`
  - `tests/unit/web/test_routes_conversations.py`
  - `tests/unit/web/test_ws_conversations.py`
  - updated `test_protocol.py`, and existing engine, ws and route tests
  - frontend: `stores/chat.*.test.ts`, `UpdateBanner.test.tsx`

---

### Task 1: Migration 0008 and `ConversationRepo`

- [ ] **Step 1: Failing tests.**
  - **`test_migration_0008.py`.** Build a DB with migrations 0001–0007,
    insert legacy data, then run `migrate`:
    - project P1: 3 messages, 2 runs, an `agent_sessions` row `sdk-1`, an
      open plan
    - project P2: runs but no messages
    - project P3: an `agent_sessions` row only

    Assert:
    - one conversation per project (`seq=1`, `title='Session 1'`,
      `type='free'`)
    - P1's `sdk_session_id == 'sdk-1'`
    - every message, run and plan row is linked, with no NULL
      `conversation_id` left
    - message ids and order unchanged
    - `agent_sessions` gone
    - `created_at` ends with `Z`
    - a second `migrate` is a no-op
  - **`test_conversations_repo.py`:**
    - `create` allocates seq 1, 2, 3; two concurrent creates get distinct seq
    - `get_for_project` with a foreign project raises
      `ConversationNotFound`
    - `set_status` transitions
    - `usage_totals` sums `runs.usage` via `json_extract` (two runs with
      usage plus one with NULL)
    - `closing()` lists rows across projects
- [ ] **Step 2:** Run the tests and see them FAIL.
- [ ] **Step 3: Implement** spec §4.2 (without the doc tables, S-C1) and
  S-C2. Title: 1–120 chars, single line (validated in the repo and the
  route models).
- [ ] **Step 4:** PASS. **Step 5:** commit `feat: conversations table and repo`.

### Task 2: Messages and runs per conversation

- [ ] **Step 1: Failing tests:**
  - `MessageRepo.page(project_id, conversation_id, limit, before)` and
    `since(…, conversation_id, since_id)` return only that conversation's
    rows.
  - `insert`, `insert_user` and `queued_user_rows` carry `conversation_id`.
  - `queued_user_rows(project_id)` still returns all of the project's rows,
    in id order, each with its conversation.
  - `RunRepo.start` stores `conversation_id`.
  - `touch` is called on insert.
- [ ] **Step 2:** FAIL. **Step 3: Implement.** `MessageDTO` gains
  `conversationId`.
- [ ] **Step 4:** PASS. **Step 5:** commit.

### Task 3: Engine bound to conversations (Review Focus 1)

- [ ] **Step 1: Failing tests** in `test_engine_conversations.py`, using
  the scripted `FakeAgent` and recording `start()` calls with conversation
  and resume:
  - **Ordering:**
    - `test_a_b_a_three_bursts_in_order`: A1 runs. B and A3 are sent while
      A1 runs. The burst order is A1, B, A3, and each `start()` gets its
      conversation's `sdk_session_id`.
    - `test_a3_after_drain_before_finally`: A3 arrives after A1's stream
      drained but before the burst's `finally`. B still goes first.
    - `test_seal_during_wind_down`: a new stream is sealed while the
      previous burst waits in `_await_wind_down`. The old burst's `finally`
      does not clear `_sealed_stream`.
  - **Session ids and cancellation:**
    - `test_session_id_stored_on_burst_conversation`.
    - `test_stop_during_sealed_stream`.
    - `test_stop_from_b_view_on_a_between_turns`: the frame carries A's
      `conversationId` (the running chip sends `activeRun.conversationId`).
      Only A's queue is cancelled.
    - `test_turn_context_in_kicadless_ctx`: a `requires_kicad=False` tool
      sees `run_id` while KiCad is stopped.
    - `test_stop_scoped`: Stop on A's run. B's queued message stays
      `queued` and runs afterwards. A's other pending items are
      `cancelled`.
    - `test_stop_between_turns_uses_frame_conversation`.
    - `test_cancel_db_queued_foreign_row`.
  - **Restart, rate limits and notes:**
    - `test_restart_two_conversations` (`resume_pending` runs both, in id
      order).
    - `test_rate_limit_parks_all`.
    - `test_broadcast_note_reaches_each_open_conversation_once` and
      `test_conversation_note_only_there`.
  - **Notices, closed sessions and hooks:**
    - `test_post_notice_into_given_conversation`.
    - `test_send_to_closed_conversation_refused` (`conversation_closed`).
    - Each `before_model_input` entry is prefixed exactly once, at its
      start, with `[CopperChat notice]`. An entry with newlines yields one
      prefix. It goes to the model item only. `TurnInfo.text` stays the user's text.
    - `on_items_failed` is called by `_fail_burst`, `cancel_queued` and the
      Stop cancellation.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement spec §5.2–§5.3 and S-C3.**
  - **Streams.** `PushStream(conversation_id)`. `seal()` behaves like
    `request_close`, but `push()` returns False from then on.
  - **Engine state.** `self._sealed_stream` (identity). `_enqueue` leaves
    the item DB-queued while `_sealed_stream is not None`, or when the item
    belongs to another conversation than `self._stream`; in the second case
    it calls `seal()` first.
  - **Burst end.** The burst's `finally` clears `_sealed_stream` only if it
    `is stream`, then calls `_drive_queued` (already at the end of
    `_run_burst`).
  - **Burst start.** `_run_burst` reads the conversation of the first item
    (`ConversationRepo.get`) and passes the row to `backend.start`.
  - **Notes.** `_notes` becomes `dict[cid, list[str]]` plus `_broadcast:
    list[str]`, with a per-conversation set of the broadcast notes already
    delivered.
  - **Callers.** Update every caller of `post_notice`, `post_error` and
    `add_agent_note`:
    - plans → `plans.conversation_id`
    - ERC hooks → the turn's conversation
    - restore → the request's conversation, with a broadcast note
    - external change and recovery → broadcast note, with the notice in the
      most recently active open conversation
- [ ] **Step 4:** PASS, plus the whole existing engine test suite (adapted
  to the new signatures). **Step 5:** commit `feat: engine bursts bound to conversations`.

### Task 4: REST and WS

- [ ] **Step 1: Failing tests:**
  - **REST:** `GET/POST/PATCH /conversations` (`type='board'` → 400, a title
    with a newline → 422), `GET /conversations/{cid}/messages` paging, and
    a foreign `cid` → 404.
  - **Restore and DTOs:** restore with a foreign `conversationId` → 404 and
    nothing restored. `/history` and `/changes/runs` include
    `conversationId` and `conversationTitle`.
  - **WS:** `subscribe` replays only that conversation, and live frames of
    the project's other conversations still arrive. `send` and `interrupt`
    with another project's `cid` give `error conversation_not_found`, and
    nothing is enqueued. `subscribe` with project B's `cid` under project A →
    `error conversation_not_found`, with no replay and no cursor bound.
    `conversation-updated` is published on the first and last message and on create,
    rename, archive and status change.
  - **Protocol:** every fixture is updated, with new invalid cases (missing
    `conversationId` for `send`, `subscribe` and `interrupt`), and the new
    `conversation-updated.json`. The backend round-trip and frontend
    `frames.test.ts` pass.
  - **Auth:** every new route returns 401 without a session.
- [ ] **Step 2:** FAIL. **Step 3: Implement** spec §6.1 (conversations,
  messages, restore) and §6.2. Push notifications put the session title
  into the text.
- [ ] **Step 4:** PASS. **Step 5:** commit.

### Task 5: Interim frontend and build-version banner

- [ ] **Step 1: Failing tests:**
  - **Chat store** (`stores/chat.*.test.ts`): it is keyed per conversation,
    `onFrame` routes by `conversationId`, and existing ordering and
    reconnect tests are adapted.
  - **ProjectPage:** it lists conversations and, if there are none, creates
    one (`type:'free'`). It opens the newest open conversation.
  - **`openPanel`:** `openPanel('history')` opens the History sheet on
    desktop and the panel on phone. `params` reach the panel.
  - **Restore:** the History restore sends the current `conversationId`.
  - **`UpdateBanner.test.tsx`:** a mismatch between `/api/health` `version`
    and the build version shows the banner. A match shows nothing.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement** S-C8 (C1). `ChatPanel` gets `conversationId`.
  - `PlanCard.runActive` reads the project-level active run.
  - `ChangesPanel`'s restore lookup uses the current conversation.
  - The build version comes from Vite `define` (`__COPPERCHAT_VERSION__`,
    read from `frontend/package.json`; the Docker frontend stage has no
    backend files). Test: equal versions keep the banner hidden. The banner
    is mounted in `App.tsx`.
- [ ] **Step 4:** PASS (full frontend command set). **Step 5:** commit.

## Acceptance

- [ ] The migration test passes. A copy of the prod DB (the owner's
  `~/copperchat-run/data`, **copied read-only**, never opened in place)
  upgrades cleanly: the counts of messages, runs and plans are unchanged,
  and there is one conversation per project with history. The coordinator
  may do this step if the child has no access; note it in the done report.
- [ ] The tests of Task 3 pass, including all three ordering timings.
- [ ] All existing backend and frontend tests are green. `main` stays
  usable: chat works in the default session.
