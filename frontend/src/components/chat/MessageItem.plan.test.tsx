import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useContext } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { ChatStoreContext } from '@/hooks/useChat';
import type { MessageDTO, PlanDTO, PlanStepDTO } from '@/protocol/frames';
import { chatKey, createChatStore } from '@/stores/chat';
import { createPlansStore, type PlansDeps, PlansStoreContext } from '@/stores/plans';
import { FakeWsClient } from '@/test/fakeWsClient';
import { CID, PID, row } from '@/test/fakeMessageServer';
import { latestPlanCards } from '@/components/plan/PlanNotice';
import { MessageItem } from './MessageItem';

const PLAN = 'p'.repeat(32);
const REASON = 'Step 2 needs a <b>second</b> resistor';

const step = (idx: number, over: Partial<PlanStepDTO> = {}): PlanStepDTO => ({
  idx, title: `Step ${idx} title`, intent: '', expectedChanges: 3, touches: { refs: [], nets: [] }, verification: '',
  status: 'pending', changeCount: 0, used: 0, checkpointId: null, summary: null, failure: null, erc: null, ...over,
});
const plan = (over: Partial<PlanDTO> = {}): PlanDTO => ({
  id: PLAN, projectId: PID, conversationId: CID, title: 'Add a status LED', summary: '', status: 'proposed', revision: 2,
  pauseReason: null, reviseReason: REASON, previousRevision: 1,
  steps: [step(1, { status: 'done', changeCount: 2 }), step(2, { status: 'paused', used: 2 })],
  createdAt: '2026-10-18T10:00:00.000Z', updatedAt: '2026-10-18T10:05:00.000Z', ...over,
});
const notice = (id: number, metadata: Record<string, unknown>, content = 'notice') =>
  row(id, { role: 'system', kind: 'notice', content, metadata });

/** Propose (revision 1), work, a mid-step revise (revision 2). */
const REVISED: MessageDTO[] = [
  row(1, { role: 'user', content: 'Add a status LED' }),
  notice(2, { action: 'plan', planId: PLAN }, 'The agent proposed a plan.'),
  row(3, { content: 'Working on step 2' }),
  notice(4, { action: 'plan-revised', planId: PLAN, revision: 2, agentReason: REASON }, 'The agent revised the plan (revision 2).'),
];

/** The chat list as ChatPanel renders it: one MessageItem per row of the chat store. */
function Rows() {
  const useStore = useContext(ChatStoreContext);
  const messages = useStore((s) => s.chats[chatKey(PID, CID)]?.messages) ?? [];
  return (
    <ul>
      {messages.map((m) => (
        <li key={m.id} data-msg-id={m.id}>
          <MessageItem message={m} />
        </li>
      ))}
    </ul>
  );
}

/** Fresh stores fed from the "API" (persisted rows and the plan), as after a page load. */
async function mount(messages: MessageDTO[], current: PlanDTO, deps: Partial<PlansDeps> = {}) {
  const ws = new FakeWsClient();
  const plans = createPlansStore(ws, {
    fetchOpen: vi.fn(async () => current),
    fetchPlan: vi.fn(async () => current),
    event: vi.fn(async () => current),
    rollback: vi.fn(async () => current),
    ...deps,
  });
  const chat = createChatStore({ ws, fetchPage: async () => ({ messages, hasMore: false }) });
  await act(async () => {
    await chat.getState().open(PID, CID);
  });
  const view = render(
    <ChatStoreContext.Provider value={chat}>
      <PlansStoreContext.Provider value={plans}>
        <Rows />
      </PlansStoreContext.Provider>
    </ChatStoreContext.Provider>,
  );
  await screen.findByRole('heading', { name: 'Add a status LED' });
  return { ws, chat, plans, view };
}

const item = (id: number) => document.querySelector<HTMLElement>(`[data-msg-id="${id}"]`)!;

/** The layout a user sees: per plan-card row, interactive (Approve / Resume) or collapsed. */
function layout() {
  return [2, 4, 5]
    .map((id) => item(id))
    .filter(Boolean)
    .map((el) => {
      const collapsed = within(el).queryByRole('button', { name: /^Superseded/ });
      if (collapsed) return `collapsed: ${collapsed.textContent}`;
      return `card: ${within(el)
        .queryAllByRole('button')
        .map((b) => b.textContent)
        .join(', ')}`;
    });
}

describe('plan cards in the chat list', () => {
  it('renders revised plan at the bottom with Approve', async () => {
    await mount(REVISED, plan());
    const last = item(4);
    expect(within(last).getByRole('heading', { name: 'Add a status LED' })).toBeInTheDocument();
    expect(within(last).getByRole('button', { name: 'Approve' })).toBeEnabled();
    expect(within(last).getByRole('button', { name: 'Reject revision' })).toBeEnabled();
    // The agent's reason is plain text, never markup.
    expect(within(last).getByText(`Revised: ${REASON}`)).toBeInTheDocument();
    expect(last.querySelector('b')).toBeNull();
    // Exactly one interactive card for the plan.
    expect(screen.getAllByRole('button', { name: 'Approve' })).toHaveLength(1);
  });

  it('older card collapses to "Superseded by revision 2"', async () => {
    await mount(REVISED, plan());
    const first = item(2);
    const toggle = within(first).getByRole('button', { name: 'Superseded by revision 2' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(within(first).queryByRole('heading')).toBeNull();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    // Read-only: the plan, but none of its actions.
    expect(within(first).getByRole('heading', { name: 'Add a status LED' })).toBeInTheDocument();
    expect(within(first).getByText('Paused – revision awaiting approval')).toBeInTheDocument();
    expect(within(first).getAllByRole('button')).toEqual([toggle]);
    fireEvent.click(toggle);
    expect(within(first).queryByRole('heading')).toBeNull();
  });

  it('survives reload', async () => {
    const first = await mount(REVISED, plan());
    const before = layout();
    expect(before).toEqual(['collapsed: Superseded by revision 2', 'card: Approve, Reject revision']);
    first.view.unmount();
    first.plans.getState().dispose();
    // A reload: new stores, rebuilt from the persisted rows and the plan from the API.
    await mount(REVISED, plan());
    expect(layout()).toEqual(before);
  });

  it('a new revision notice moves the interactive card to the bottom', async () => {
    const { ws, chat } = await mount(REVISED.slice(0, 3), plan({ revision: 1, reviseReason: null, previousRevision: null, steps: [step(1), step(2)] }));
    expect(within(item(2)).getByRole('button', { name: 'Approve' })).toBeInTheDocument();
    act(() => {
      ws.emit({ type: 'plan-updated', projectId: PID, plan: plan({ updatedAt: '2026-10-18T10:06:00.000Z' }) });
      ws.emit({ type: 'message', projectId: PID, message: REVISED[3]! });
    });
    await waitFor(() => expect(within(item(4)).getByRole('button', { name: 'Approve' })).toBeInTheDocument());
    expect(within(item(2)).getByRole('button', { name: 'Superseded by revision 2' })).toBeInTheDocument();
    expect(chat.getState().chats[chatKey(PID, CID)]!.messages).toHaveLength(4);
  });

  it('a plan-restored notice re-posts the plan paused with Resume', async () => {
    const restored = plan({
      status: 'paused',
      revision: 1,
      previousRevision: null,
      reviseReason: null,
      pauseReason: 'interrupted',
      steps: [step(1, { status: 'done', changeCount: 2 }), step(2, { status: 'paused', used: 2 })],
    });
    await mount(
      [...REVISED, notice(5, { action: 'plan-restored', planId: PLAN, revision: 1 }, 'Revision 2 was rejected.')],
      restored,
    );
    const last = item(5);
    expect(within(last).getByText('Revision rejected – back to revision 1, paused.')).toBeInTheDocument();
    expect(within(last).getByRole('button', { name: 'Resume' })).toBeEnabled();
    expect(within(within(last).getAllByRole('listitem')[1]!).getByText('Paused')).toBeInTheDocument();
    expect(layout()).toEqual([
      'collapsed: Superseded by revision 1',
      'collapsed: Superseded by revision 1',
      'card: Resume, Roll back to…, Abort',
    ]);
  });

  it('a plan-revised notice without a plan id stays a plain notice', async () => {
    render(<MessageItem message={notice(9, { action: 'plan-revised', agentReason: REASON }, 'The agent revised the plan.')} />);
    expect(screen.getByText('The agent revised the plan.')).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('latestPlanCards maps each plan to its last card row and is cached per list', () => {
    const other = 'q'.repeat(32);
    const list = [...REVISED, notice(5, { action: 'plan', planId: other }), notice(6, { action: 'erc-delta', planId: PLAN })];
    const map = latestPlanCards(list);
    expect([...map]).toEqual([
      [PLAN, 4],
      [other, 5],
    ]);
    expect(latestPlanCards(list)).toBe(map);
  });
});
