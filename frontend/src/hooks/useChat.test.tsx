import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { createChatStore, fetchMessagesPage, type ChatStore } from '@/stores/chat';
import { FakeMessageServer, PID, row } from '@/test/fakeMessageServer';
import { FakeWsClient } from '@/test/fakeWsClient';
import { ChatStoreContext, useChat } from './useChat';

let ws: FakeWsClient;
let store: ChatStore;
let server: FakeMessageServer;
const wrapper = ({ children }: { children: ReactNode }) => (
  <ChatStoreContext.Provider value={store}>{children}</ChatStoreContext.Provider>
);

beforeEach(() => {
  ws = new FakeWsClient();
  ws.open = true;
  server = new FakeMessageServer([
    row(1, { role: 'user', status: 'done' }),
    row(2, { role: 'user', status: 'queued' }),
    row(3, { role: 'assistant', status: 'queued' }),
  ]).install();
  store = createChatStore({ ws, fetchPage: fetchMessagesPage, newClientMessageId: () => 'c1' });
});

describe('useChat', () => {
  it('opens the chat on mount (ensuring the socket) and derives queued from server user rows only', async () => {
    const { result } = renderHook(() => useChat(PID), { wrapper });
    expect(result.current.loaded).toBe(false);
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(ws.ensureCalls).toBeGreaterThan(0);
    act(() => result.current.send('pending'));
    expect(result.current.messages.map((m) => m.id)).toEqual([1, 2, 3, -1]);
    expect(result.current.queued.map((m) => m.id)).toEqual([2]);
    act(() => result.current.cancelQueued(2));
    expect(ws.framesOfType('cancel-queued')).toEqual([{ type: 'cancel-queued', projectId: PID, messageId: 2 }]);
    expect(result.current.queued).toEqual([]);
  });

  it('keeps the queued array identity while unrelated rows stream', async () => {
    const { result } = renderHook(() => useChat(PID), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    const before = result.current.queued;
    act(() => ws.emit({ type: 'message-delta', projectId: PID, messageId: 3, contentDelta: ' more', statusPatch: 'streaming' }));
    expect(result.current.messages.find((m) => m.id === 3)?.content).toBe('m3 more');
    expect(result.current.queued).toBe(before);
    act(() => ws.emit({ type: 'message-delta', projectId: PID, messageId: 2, statusPatch: 'streaming' }));
    expect(result.current.queued).toEqual([]);
  });

  it('two components on the same project share one baseline load', async () => {
    renderHook(() => [useChat(PID), useChat(PID)], { wrapper });
    await waitFor(() => expect(store.getState().chats[PID]?.loaded).toBe(true));
    expect(server.messageCalls()).toHaveLength(1);
    expect(ws.framesOfType('subscribe')).toHaveLength(1);
  });
});
