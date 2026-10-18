# Phase E9b — Plan card, history nesting, Settings › Editing

> Index contracts: E-C11, E-C12, E-C13. Spec §4 (frontend), §7.3 (plan card).
> Frontend conventions: foundation C7a (chat rendering via `MessageItem` /
> `ChatPanel.renderItem`, `RestoreOffer`, `ConfirmRestore`, `registerPanel`).

**Goal:** the user approves, follows, pauses, resumes, rolls back or aborts plans
from a card in the chat; sees step checkpoints nested in History; sets the editing
policy per project.

**Files:**
- Create: `frontend/src/stores/plans.ts`, `frontend/src/components/plan/{PlanCard,PlanStepRow,ConfirmRollback}.tsx`,
  `frontend/src/pages/settings/EditingSection.tsx`
- Modify: `components/chat/MessageItem.tsx` (notice `action === 'plan'` → `PlanCard`;
  `action === 'erc-delta'` → `ErcBadge` from E9a; `restore` with `checkpoint` → label
  "Restore step checkpoint"), `panels/HistoryPanel.tsx`, `pages/SettingsPage.tsx`
- Tests: colocated `*.test.tsx`; Playwright `frontend/e2e/smoke.spec.ts` (mocked plan flow, phone + desktop)

---

### Task 1: Plans store and plan card

**Interfaces:** `@/stores/plans` (`createPlansStore(ws)`, `byId`, `open`, `load(projectId)`,
`approve|reject|resume|abort(planId)`, `rollback(planId, toStep)`; updates from
`plan-updated`); `PlanCard({projectId, planId})`.

- [ ] **Step 1: Failing Vitest tests** (`PlanCard.test.tsx`, fake http + ws):
  `proposed shows Approve and Reject`; `approve posts once and disables buttons while pending`;
  `running shows Stop (interrupt frame) and per-step status`; `paused shows Resume, Roll back to…, Abort`
  (rollback opens a confirm listing the checkpoints; 409 `agent_busy` shown inline);
  `done step shows checkpoint link, change count, ERC justifications`;
  `card updates from plan-updated without reload`; `revision badge after plan_revise`;
  `terminal states show no buttons`; phone layout: buttons full width (class check).
- [ ] **Step 2:** FAIL. **Step 3: Implement.** **Step 4:** PASS. **Step 5:** commit `feat: plan card`.

### Task 2: History nesting

**Interfaces:** `HistoryEntry.kind`, `.planStepId`; step checkpoints nested under the turn
snapshot with the same `runId`.

- [ ] **Step 1: Failing test** (`HistoryPanel.test.tsx`): steps render indented under their
  turn, each with its own restore button (same `ConfirmRestore`); a step without its turn
  (pruned edge case) renders top-level.
- [ ] **Step 2:** FAIL. **Step 3: Implement.** **Step 4:** PASS. **Step 5:** commit.

### Task 3: Settings › Editing

**Interfaces:** `EditingSection` using `GET|PUT /projects/{id}/settings/editing` for
`useProjects().current`.

- [ ] **Step 1: Failing test** (`SettingsPage.test.tsx`): without an open project the section
  says "Open a project to change its editing settings"; with one it shows plan policy
  (`always` / `large changes` (default) / `never`), change budget (default 20, 1–500),
  annotation block (×100 / ×1000), "Show agent view after each step" (on); a change PUTs only
  that field; server 400 shown inline.
- [ ] **Step 2:** FAIL. **Step 3: Implement.** **Step 4:** PASS, then `npm run build` + Playwright
  smoke (mocked plan propose → approve → done flow on desktop and phone).
  **Step 5:** commit `feat: editing settings and history nesting`.

---

**Done when:** one PR, CI green, review-loop done.
