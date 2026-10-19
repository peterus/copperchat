# Phase C4: Docs panel and doc notices

> **Index contracts:** S-C4 (docs routes, `doc-updated`), S-C8 (C4 part).
> **Spec sections:** §12.2, §12.3, §7.3 (user-facing semantics).
> **Runs in parallel with C2 and C5.** Frontend only. In
> `components/chat/MessageItem.tsx`, add only the `doc-changed` case. In
> `layouts/ProjectLayout.tsx`, add only the `wide` option of `PanelSheet`.
> `openPanel` (C1) and `getDocIndex` in `api/docs.ts` (C3) already exist:
> use them and extend `api/docs.ts`.

**Goal:** The user reads, edits, approves and diffs the design docs on
desktop and phone, never loses an edit to a conflict, and sees agent
changes as cards in the chat.

**Files:**
- Create:
  - `frontend/src/stores/docs.ts` (the `api/docs.ts` from C3 is extended)
  - `frontend/src/panels/DocsPanel.tsx`, `frontend/src/panels/docs.ts`
    (registration: id `docs`, order 12, `wide: true`)
  - `frontend/src/components/docs/{DocIndex,DocView,DocEditor,DocHistory,LineDiff,DocChangedCard}.tsx`
    + tests
- Modify:
  - `frontend/src/panels/index.ts`
  - `frontend/src/layouts/panels.ts` (an optional `wide` flag)
  - `frontend/src/layouts/ProjectLayout.tsx` (`PanelSheet` honours `wide`
    → `max-w-3xl`)
  - `frontend/src/components/chat/MessageItem.tsx` (the `doc-changed` case)
  - `frontend/src/components/chat/Markdown.tsx` (an optional
    `onRelativeLink`, so `../parts/<MPN>/notes.md` opens the Parts panel;
    other relative links render as plain text)
- Tests: component tests; the Playwright phone flow in `frontend/e2e/docs.spec.ts`

---

### Task 0: `openPanel` params

- [ ] TDD: `DocsPanel` honours `PanelProps.params`.
  - `{path}` opens that doc.
  - `{path, revisionId}` opens its history diff.
  - No params opens the index.

  Relative links `../parts/<MPN>/notes.md` call `openPanel('parts',
  {mpn})`. The Parts panel (`frontend/src/panels/parts.ts` and its page,
  added to C4's files) opens that part when `params.mpn` is set. Commit.

### Task 1: API, store, index and view

- [ ] **Step 1: Failing tests:**
  - **Store:** the index loads (fixed docs first, then extra docs, then
    `ignored` entries with their reason). `doc-updated` refreshes the index
    and the open doc.
  - **Index:** badges for missing, draft, approved and changed since
    approval; the user-edited marker; the open-points count; the last
    author and session.
  - **View:** rendered through `Markdown`. [Approve] calls approve with the
    shown version, and a 409 shows "changed — reload". [Unapprove]. Neither
    button appears for `decisions.md`.
- [ ] **Step 2:** FAIL. **Step 3: Implement.** **Step 4:** PASS.
  **Step 5:** commit.

### Task 2: Editor with conflict flow

- [ ] **Step 1: Failing tests:**
  - The textarea and Preview toggle. The frontmatter is not shown.
  - Save sends PUT with `baseVersion`.
  - A 409 shows "Changed while you were editing (by …)", with three
    choices:
    - [Show their change]: a diff from base to current
    - [Keep editing on top of current]: after a confirm, the base becomes
      the current version and the textarea text is kept
    - [Discard mine]
  - A `doc-updated` frame while editing shows the banner, and the textarea
    is not replaced.
  - After saving an approved doc, the panel asks "Approve again?".
  - [Let the agent rewrite] calls `clear-user-edited` when `user_edited` is
    set.
- [ ] **Step 2:** FAIL. **Step 3: Implement.** **Step 4:** PASS.
  **Step 5:** commit.

### Task 3: History, diff and revert

- [ ] **Step 1: Failing tests:** revisions newest first (author, session,
  summary, +/−). `LineDiff` is a unified line diff with green additions,
  red removals and collapsed context. The algorithm:
  - Trim the common prefix and suffix.
  - Run Myers O(ND) on the rest. Stop at D > 2,000 with "too large to
    diff". Never an n×m table.

  A perf test: two 4,000-line docs with 50 changes diff in under 200 ms in
  jsdom. A second perf test near the cap (D ≈ 1,900): the Myers trace uses
  linear-space or sliced `Int32Array` storage, never a full V array per
  step, and stays under 32 MB. [Revert to this] → POST revert with
  `baseVersion`, and a 409 goes through the conflict flow.
- [ ] **Step 2:** FAIL. **Step 3: Implement.** **Step 4:** PASS.
  **Step 5:** commit.

### Task 4: `doc-changed` card and phone layout

- [ ] **Step 1: Failing tests:**
  - `MessageItem` renders `DocChangedCard` for
    `metadata.action==='doc-changed'`: path, +/−, sections, and [View diff],
    which calls `openPanel('docs', {path, revisionId})`. A `message-delta` metadata patch
    updates the card in place.
  - Phone width: the panel is full-screen, with index → doc → history
    navigation (back arrow), a full-viewport editor, and a single-column
    diff.
- [ ] **Step 2:** FAIL. **Step 3: Implement.** **Step 4:** PASS.
  **Step 5:** commit.
- [ ] **e2e** `docs.spec.ts` at phone width: open Docs under "More", edit
  `requirements.md`, save, approve, open History and see the diff.

## Acceptance

- [ ] The frontend command set is green, and the e2e test passes.
- [ ] No user edit can be lost silently: every 409 path is covered by a
  test.
