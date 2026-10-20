import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ChatStoreContext } from '@/hooks/useChat';
import type { PlanDTO } from '@/protocol/frames';
import { createChatStore, type ChatStore } from '@/stores/chat';
import { useComposer } from '@/stores/composer';
import { ConversationsStoreContext } from '@/stores/conversations';
import { createPlansStore, PlansStoreContext } from '@/stores/plans';
import { conv } from '@/test/conv';
import { PID, row } from '@/test/fakeMessageServer';
import { fakeConversations } from '@/test/sessions';
import { ChatPanel } from './ChatPanel';

const C1 = '11111111111111111111111111111111';
const C2 = '22222222222222222222222222222222';

let f: ReturnType<typeof fakeConversations>;
let chat: ChatStore;
let rows: ReturnType<typeof row>[];

const plan = (conversationId: string, status: PlanDTO['status'] = 'proposed'): PlanDTO => ({
  id: 'p1',
  projectId: PID,
  conversationId,
  title: 'Add LDO',
  summary: '',
  status,
  revision: 1,
  pauseReason: null,
  steps: [],
  createdAt: '2026-10-19T10:00:00Z',
  updatedAt: '2026-10-19T10:00:00Z',
});
let openPlan: PlanDTO | null;

beforeEach(async () => {
  vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} unobserve() {} takeRecords() { return []; } });
  useComposer.getState().reset();
  f = fakeConversations([conv(C1, 1), conv(C2, 2, { title: 'Power stage', type: 'schematic' })]);
  await f.store.getState().load(PID);
  rows = [];
  chat = createChatStore({ ws: f.ws, fetchPage: async () => ({ messages: rows, hasMore: false }), newClientMessageId: () => 'c1' });
  openPlan = null;
});

function renderPanel(cid = C1) {
  const plans = createPlansStore(f.ws, {
    fetchOpen: async () => openPlan,
    fetchPlan: async () => openPlan!,
    event: async () => openPlan!,
    rollback: async () => openPlan!,
  });
  void plans.getState().load(PID); // done by ProjectPage
  return render(
    <ConversationsStoreContext.Provider value={f.store}>
      <PlansStoreContext.Provider value={plans}>
        <ChatStoreContext.Provider value={chat}>
          <MemoryRouter>
            <ChatPanel projectId={PID} conversationId={cid} />
          </MemoryRouter>
        </ChatStoreContext.Provider>
      </PlansStoreContext.Provider>
    </ConversationsStoreContext.Provider>,
  );
}

describe('ChatPanel with sessions', () => {
  it('shows the session header above the messages', async () => {
    renderPanel();
    expect(await screen.findByRole('heading', { name: 'Session 1' })).toBeInTheDocument();
  });

  it('a closed session hides the composer and offers Reopen', async () => {
    f.rows[0] = { ...f.rows[0]!, status: 'closed' };
    await f.store.getState().load(PID);
    renderPanel();
    await screen.findByRole('heading', { name: 'Session 1' });
    expect(screen.queryByRole('textbox', { name: 'Message' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Reopen session' }));
    await waitFor(() => expect(f.api.reopen).toHaveBeenCalledWith(PID, C1));
    expect(await screen.findByRole('textbox', { name: 'Message' })).toBeInTheDocument();
  });

  it('Close session → closing: the composer is disabled until the handover finishes', async () => {
    renderPanel();
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Close session' }));
    await waitFor(() => expect(screen.getByLabelText('Session status')).toHaveTextContent('closing'));
    expect(screen.getByRole('textbox', { name: 'Message' })).toBeDisabled();
    act(() => f.ws.emit({ type: 'conversation-updated', projectId: PID, conversation: conv(C1, 1, { status: 'closed' }) }));
    expect(screen.queryByRole('textbox', { name: 'Message' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Reopen session' })).toBeInTheDocument();
  });

  it('a refused reopen shows the error', async () => {
    f.rows[0] = { ...f.rows[0]!, status: 'closed' };
    await f.store.getState().load(PID);
    f.api.reopen.mockRejectedValueOnce(new Error('Not closed'));
    renderPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'Reopen session' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Not closed');
  });

  it('banner "Plan open in <session>" when the open plan belongs to another session', async () => {
    openPlan = plan(C2);
    renderPanel();
    const banner = await screen.findByRole('note');
    expect(banner).toHaveTextContent('Plan open in Power stage');
    expect(within(banner).getByRole('link', { name: 'Power stage' })).toHaveAttribute('href', `/p/${PID}/s/${C2}`);
  });

  it('no banner for the own plan or a finished one', async () => {
    openPlan = plan(C1);
    const { unmount } = renderPanel();
    await screen.findByRole('heading', { name: 'Session 1' });
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByRole('note')).toBeNull();
    unmount();
    act(() => f.ws.emit({ type: 'plan-updated', projectId: PID, plan: plan(C2, 'done') }));
    renderPanel();
    await screen.findByRole('heading', { name: 'Session 1' });
    expect(screen.queryByText(/Plan open in/)).toBeNull();
  });

  it('a message queued while another session runs shows "waiting for <session>"', async () => {
    rows = [row(4, { conversationId: C1, role: 'user', status: 'queued', content: 'next please' })];
    renderPanel();
    expect(await screen.findByRole('list', { name: 'Queued messages' })).toHaveTextContent('next please');
    expect(screen.queryByText(/waiting for/i)).toBeNull();
    act(() => f.ws.emit({ type: 'run-started', projectId: PID, conversationId: C2, runId: 'r1' }));
    expect(screen.getByText('Waiting for Power stage')).toBeInTheDocument();
    act(() => f.ws.emit({ type: 'run-finished', projectId: PID, conversationId: C2, runId: 'r1', reason: 'completed' }));
    expect(screen.queryByText(/waiting for/i)).toBeNull();
  });

  it('the close-session message is a compact "Closing session…" row, also when queued', async () => {
    rows = [
      row(5, { conversationId: C1, role: 'user', content: 'Close this session: bring the design documents up to date', metadata: { origin: 'close-session' } }),
      row(6, { conversationId: C1, role: 'user', status: 'queued', content: 'Close this session: …', metadata: { origin: 'close-session' } }),
    ];
    renderPanel();
    const log = await screen.findByRole('log');
    await waitFor(() => expect(within(log).getByText('Closing session…')).toBeInTheDocument());
    expect(within(log).queryByText(/bring the design documents/)).toBeNull();
    expect(screen.getByRole('list', { name: 'Queued messages' })).toHaveTextContent('Closing session…');
  });
});
