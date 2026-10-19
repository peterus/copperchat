import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { act } from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { ChatStoreContext, useChat } from '@/hooks/useChat';
import { chatKey, createChatStore, fetchMessagesPage, type ChatStore } from '@/stores/chat';
import { CID as C, FakeMessageServer, PID } from '@/test/fakeMessageServer';
import { FakeWsClient } from '@/test/fakeWsClient';
import { RunControls } from './RunControls';

const P = PID;
let ws: FakeWsClient;
let store: ChatStore;

function Harness() {
  const chat = useChat(P, C);
  return <RunControls runActive={chat.runActive} onStop={chat.stop} />;
}

beforeEach(() => {
  ws = new FakeWsClient();
  ws.open = true;
  new FakeMessageServer([]).install();
  store = createChatStore({ ws, fetchPage: fetchMessagesPage });
});

async function mount() {
  render(
    <ChatStoreContext.Provider value={store}>
      <Harness />
    </ChatStoreContext.Provider>,
  );
  await waitFor(() => expect(store.getState().chats[chatKey(P, C)]?.loaded).toBe(true));
}

describe('Stop button', () => {
  it('appears for an active run, interrupts it, and never sticks after a reconnect that lost run-finished', async () => {
    await mount();
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
    act(() => ws.emit({ type: 'run-started', projectId: P, conversationId: C, runId: 'r1' }));
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(ws.framesOfType('interrupt')).toEqual([{ type: 'interrupt', projectId: P, conversationId: C, runId: 'r1' }]);
    act(() => {
      ws.drop();
      ws.connect();
    });
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
    act(() => ws.emit({ type: 'run-started', projectId: P, conversationId: C, runId: 'r1' }));
    expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument();
  });

  it('disappears when the server refuses the socket (logged out) and stays hidden without a reconnect', async () => {
    await mount();
    act(() => ws.emit({ type: 'run-started', projectId: P, conversationId: C, runId: 'r1' }));
    act(() => ws.refuse('unauthorized'));
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
  });

  it('hides once the run finishes', async () => {
    await mount();
    act(() => ws.emit({ type: 'run-started', projectId: P, conversationId: C, runId: 'r1' }));
    act(() => ws.emit({ type: 'run-finished', projectId: P, conversationId: C, runId: 'r1', reason: 'interrupted' }));
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
  });
});
