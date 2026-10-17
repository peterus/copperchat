import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ChatStoreContext } from '@/hooks/useChat';
import { createChatStore, fetchMessagesPage, type ChatStore } from '@/stores/chat';
import { useComposer } from '@/stores/composer';
import { FakeMessageServer, PID, row } from '@/test/fakeMessageServer';
import { FakeWsClient } from '@/test/fakeWsClient';
import { ChatPanel } from './ChatPanel';

let ws: FakeWsClient;
let store: ChatStore;

let server: FakeMessageServer;

function setup(rows = [row(1, { content: 'Welcome to the board' }), row(2, { role: 'user', status: 'queued', content: 'later' })]) {
  server = new FakeMessageServer(rows).install();
  store = createChatStore({ ws, fetchPage: fetchMessagesPage, newClientMessageId: () => 'c1' });
}

beforeEach(() => {
  vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} unobserve() {} takeRecords() { return []; } });
  useComposer.getState().clear();
  ws = new FakeWsClient();
  ws.open = true;
});

function renderPanel(props: { readOnly?: boolean } = {}) {
  return render(<ChatStoreContext.Provider value={store}><ChatPanel projectId={PID} {...props} /></ChatStoreContext.Provider>);
}

describe('ChatPanel', () => {
  it('renders history, keeps queued rows in the composer queue, sends and shows usage/rate limit', async () => {
    setup();
    renderPanel();
    expect(await screen.findByText('Welcome to the board')).toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'Queued messages' })).toHaveTextContent('later');
    expect(screen.getByRole('log')).not.toHaveTextContent('later');

    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), { target: { value: 'What is R12?' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(ws.framesOfType('send')).toEqual([{ type: 'send', projectId: PID, clientMessageId: 'c1', text: 'What is R12?', attachments: [] }]);
    expect(screen.getByText('What is R12?')).toBeInTheDocument();

    act(() => ws.emit({ type: 'usage', projectId: PID, usage: { inputTokens: 1500, outputTokens: 20, cacheReadTokens: 0, cacheCreationTokens: 0 } }));
    expect(screen.getByLabelText('Token usage')).toHaveTextContent('in 1.5k');
    act(() => ws.emit({ type: 'rate-limited', projectId: PID, resetsAt: new Date(Date.now() + 3_600_000).toISOString() }));
    expect(screen.getByRole('status')).toHaveTextContent('Rate limit reached');
  });

  it('shows and dismisses errors', async () => {
    setup();
    renderPanel();
    await waitFor(() => expect(store.getState().chats[PID]?.loaded).toBe(true));
    act(() => ws.emit({ type: 'error', projectId: PID, code: 'internal_error', message: 'Something broke' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Something broke');
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss error' }));
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('nests research_part sub-agent rows in a card and shows Stop while the run is active', async () => {
    setup([
      row(1, { role: 'user', content: 'Research TPS62133' }),
      row(2, { kind: 'tool_use', content: 'research_part', metadata: { toolUseId: 'a1', name: 'research_part', input: { mpn: 'TPS62133', query: 'layout?' } } }),
      row(3, { kind: 'tool_use', content: 'WebSearch', metadata: { parentToolUseId: 'a1', toolUseId: 'w1', name: 'WebSearch', input: { query: 'TPS62133 datasheet' } } }),
      row(4, { kind: 'text', content: 'orphan from an older delegation', metadata: { parentToolUseId: 'gone' } }),
    ]);
    renderPanel();
    await screen.findByText('Part research: TPS62133');
    act(() => ws.emit({ type: 'run-started', projectId: PID, runId: 'r1' }));
    const log = screen.getByRole('log');
    const card = screen.getByText('Part research: TPS62133').closest('details')!;
    expect(card).toHaveAttribute('open');
    expect(within(card).getByText('Searched: TPS62133 datasheet')).toBeInTheDocument();
    expect(log.querySelectorAll('[data-msg-id]')).toHaveLength(3); // user, card, orphan
    expect(within(log).getByText('orphan from an older delegation')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument();

    act(() => ws.emit({ type: 'message', projectId: PID, message: row(5, { kind: 'tool_result', content: 'Buck, 3 MHz', metadata: { toolUseId: 'a1', isError: false, images: [] } }) }));
    act(() => ws.emit({ type: 'run-finished', projectId: PID, runId: 'r1', reason: 'completed' }));
    expect(within(card).getByText('done')).toBeInTheDocument();
    expect(card).not.toHaveAttribute('open');
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
  });

  it('shows tool images once (on the tool result) and error rows inline', async () => {
    const url = `/api/projects/${PID}/images/a.png`;
    setup([
      row(1, { kind: 'tool_result', content: 'rendered', metadata: { toolUseId: 't1', isError: false, images: [{ url }] } }),
      row(2, { kind: 'image', content: '', metadata: { toolUseId: 't1', url } }),
      row(3, { kind: 'error', content: 'KiCad is not running', metadata: { code: 'kicad_unavailable' } }),
    ]);
    renderPanel();
    expect(await screen.findByText('KiCad is not running')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /Open image/ })).toHaveLength(1);
    expect(screen.getByRole('log').querySelectorAll('[data-msg-id]')).toHaveLength(2);
  });

  it('shows the read-only hint', async () => {
    setup([]);
    renderPanel({ readOnly: true });
    expect(screen.getByText(/Read-only/)).toBeInTheDocument();
  });

  it('a failed initial load shows a Retry button that loads the history', async () => {
    setup();
    server.failNext = 1;
    renderPanel();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not load messages');
    expect(screen.getByRole('textbox', { name: 'Message' })).toBeDisabled();
    fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Welcome to the board')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByRole('textbox', { name: 'Message' })).toBeEnabled();
  });

  it('a failed initial load is retried when the socket (re)opens', async () => {
    setup();
    server.failNext = 1;
    renderPanel();
    await screen.findByRole('alert');
    act(() => ws.connect());
    expect(await screen.findByText('Welcome to the board')).toBeInTheDocument();
  });

  it('an over-length message is refused locally: error shown, draft kept, nothing sent', async () => {
    setup();
    renderPanel();
    await screen.findByText('Welcome to the board');
    const box = screen.getByRole('textbox', { name: 'Message' });
    const long = 'x'.repeat(100_001);
    fireEvent.change(box, { target: { value: long } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(ws.framesOfType('send')).toEqual([]);
    expect(screen.getByRole('alert')).toHaveTextContent('Message not sent');
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
    expect(box).toHaveValue(long);
  });
});
