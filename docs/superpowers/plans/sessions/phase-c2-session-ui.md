# Phase C2: Session UI

> **Index contracts:** S-C4 (consumer, including the C5 close/reopen
> shapes), S-C8.
> **Spec sections:** §11 (dialog part), §12.1, §12.4, §6.3 (deep link).
> **Runs in parallel with C4 and C5.** Frontend only, plus a one-line change
> to the push URL in `push/notifier.py`. Do not touch `components/docs/*`,
> `panels/DocsPanel*` or the `doc-changed` notice case.

**Goal:** The user sees, creates, renames, archives, closes and reopens
sessions. They switch between sessions while another one runs, and they
see which session is running. History and Changes show which session made
each entry.

**Files:**
- Create: `frontend/src/stores/conversations.ts`,
  `frontend/src/components/sessions/{SessionHeader,SessionList,NewSessionDialog,TypeIcon}.tsx` + tests
- Modify:
  - Routing and pages: `frontend/src/App.tsx` (route `/p/:projectId/s/:cid`),
    `frontend/src/pages/ProjectPage.tsx` (redirect to the newest open
    session, else the empty state; replaces C1's interim auto-create)
  - Chat: `frontend/src/components/chat/ChatPanel.tsx` (header; the
    read-only composer when `closed`; the "Session N running" chip),
    `frontend/src/components/chat/Composer.tsx` (the `waiting for Session A`
    hint), `frontend/src/components/chat/MessageItem.tsx` (the
    `metadata.origin==='close-session'` row as a compact "Closing session…"
    system row)
  - Store: `frontend/src/stores/composer.ts` (draft keyed per conversation)
  - History and Changes: `frontend/src/panels/HistoryPanel.tsx`,
    `frontend/src/panels/ChangesPanel.tsx` (session chip; the Changes
    filter "this session / all", default all)
  - `frontend/src/components/history/ConfirmRestore.tsx` (names the
    session of the turn)
  - `backend/src/copperchat/push/notifier.py` (URL `/p/{pid}/s/{cid}`) and
    its test
- Tests: component tests next to each component; a store test; Playwright
  `frontend/e2e/sessions.spec.ts` (desktop and phone width)

---

### Task 1: Conversations store and routing

- [ ] **Step 1: Failing tests.**
  - **Store:** `conversations.ts` loads a project's list. The
    `conversation-updated` frame upserts it. `run-started` and
    `run-finished` set and clear `activeRun[pid]`. Unread is set when a
    `message` frame arrives for a conversation that is not open.
  - **Routing:** `/p/X` redirects to the newest open session.
    `/p/X/s/<foreign cid>` shows "Session not found" with a link back. An
    empty project shows the "Start a session" empty state.
- [ ] **Step 2:** FAIL. **Step 3: Implement.** **Step 4:** PASS.
  **Step 5:** commit.

### Task 2: Session header, list and new-session dialog

- [ ] **Step 1: Failing tests:**
  - **Header:** type icon, inline rename (1–120 chars, single line), status
    chip, a running chip with Stop for another session (it sends
    `interrupt` with the current `conversationId`), [New session] and
    [Close session].
  - **List:** sorted by activity, with running and unread dots, cost,
    archive/unarchive, "Show archived", and closed sessions dimmed. Popover
    on desktop, full-height sheet at phone width.
  - **Dialog:**
    - type cards Requirements, Schematic and Free; Board greyed out
    - the default type is Requirements when `GET /docs` reports
      `requirements.md` missing, else Schematic
    - **gate warning** (spec §11): when the type is Schematic and
      requirements are missing or not approved, it shows a warning with
      [Start anyway] and [Open requirements] (opens the `docs` panel, or
      only navigates once C4 lands; feature-detect via `usePanels()`)
  - **Close:** [Close session] → `POST …/close` → the status chip shows
    `closing`. A `closed` session hides the composer and shows [Reopen]
    (`POST …/reopen`). Both are mocked at the API layer, using the S-C4
    shapes.
- [ ] **Step 2:** FAIL. **Step 3: Implement.** **Step 4:** PASS.
  **Step 5:** commit.

### Task 3: Session chips in History and Changes; push URL

- [ ] **Step 1: Failing tests:**
  - History entries show the session chip. The restore confirm names the
    session.
  - The Changes filter works.
  - The backend notifier test expects the URL `/p/{pid}/s/{cid}`.
- [ ] **Step 2:** FAIL. **Step 3: Implement.** **Step 4:** PASS.
  **Step 5:** commit.

### Task 4: e2e

- [ ] `frontend/e2e/sessions.spec.ts`, using the fake agent (dev fake mode):
  create a Requirements session, send a message, create a Free session,
  send while the first one runs, see the queued hint, and see each reply
  land in its own session. Phone width: open the list sheet and switch
  sessions.

## Acceptance

- [ ] The frontend command set is green, and the e2e test passes in CI.
- [ ] Manual check (or Playwright screenshot) at desktop and phone width:
  switching sessions while one runs. The queued message in B runs after A,
  and its reply lands in B.
