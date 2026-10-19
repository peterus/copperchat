import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CID, FakeMessageServer, PID, row } from '@/test/fakeMessageServer';
import { FakeWsClient } from '@/test/fakeWsClient';
import { chatKey, createChatStore, fetchMessagesPage, type ChatStore } from './chat';

const P = PID;
const C1 = CID;
const C2 = 'fedcba9876543210fedcba9876543210';
const C3 = 'abababababababababababababababab';
const K1 = chatKey(P, C1);
const K2 = chatKey(P, C2);
let ws: FakeWsClient;
let server: FakeMessageServer;
let store: ChatStore;
let n = 0;
const ids = (key: string) => store.getState().chats[key]?.messages.map((m) => m.id) ?? [];

beforeEach(async () => {
  n = 0;
  ws = new FakeWsClient();
  ws.open = true;
  server = new FakeMessageServer([
    row(1),
    row(2, { conversationId: C2 }),
    row(3),
    row(4, { conversationId: C2 }),
  ]).install();
  store = createChatStore({ ws, fetchPage: fetchMessagesPage, newClientMessageId: () => `c${++n}` });
  await store.getState().open(P, C1);
});

describe('chat store keyed per conversation', () => {
  it('keys chats by `${projectId}/${conversationId}` and loads only that conversation', () => {
    expect(K1).toBe(`${P}/${C1}`);
    expect(Object.keys(store.getState().chats)).toEqual([K1]);
    expect(ids(K1)).toEqual([1, 3]);
    expect(server.messageCalls()).toEqual([`/api/projects/${P}/conversations/${C1}/messages?limit=50`]);
    expect(ws.framesOfType('subscribe')).toEqual([{ type: 'subscribe', projectId: P, conversationId: C1, sinceMessageId: 3 }]);
    expect(store.getState().current[P]).toBe(C1);
  });

  it('routes live frames by conversationId; frames of conversations not loaded are ignored', async () => {
    await store.getState().open(P, C2);
    ws.emit({ type: 'message', projectId: P, message: row(5, { conversationId: C2, status: 'streaming', content: 'a' }) });
    ws.emit({ type: 'message', projectId: P, message: row(6) });
    ws.emit({ type: 'message-delta', projectId: P, conversationId: C2, messageId: 5, contentDelta: 'b' });
    expect(ids(K1)).toEqual([1, 3, 6]);
    expect(ids(K2)).toEqual([2, 4, 5]);
    expect(store.getState().chats[K2]!.messages.at(-1)!.content).toBe('ab');
    const before = store.getState().chats;
    ws.emit({ type: 'message', projectId: P, message: row(7, { conversationId: C3 }) });
    ws.emit({ type: 'usage', projectId: P, conversationId: C3, usage: { inputTokens: 1, outputTokens: 1, cacheReadTokens: 0, cacheCreationTokens: 0 } });
    expect(store.getState().chats).toBe(before);
  });

  it('opening another conversation moves the replay cursor; reconnect subscribes only the current one', async () => {
    await store.getState().open(P, C2);
    expect(ws.framesOfType('subscribe').at(-1)).toEqual({ type: 'subscribe', projectId: P, conversationId: C2, sinceMessageId: 4 });
    await store.getState().open(P, C1); // already loaded: re-subscribes without a new GET
    expect(ws.framesOfType('subscribe').at(-1)).toEqual({ type: 'subscribe', projectId: P, conversationId: C1, sinceMessageId: 3 });
    expect(server.messageCalls()).toHaveLength(2);
    const subscribes = ws.framesOfType('subscribe').length;
    ws.drop();
    ws.connect();
    expect(ws.framesOfType('subscribe').slice(subscribes)).toEqual([
      { type: 'subscribe', projectId: P, conversationId: C1, sinceMessageId: 3 },
    ]);
    // the other loaded conversation catches up over HTTP
    await vi.waitFor(() =>
      expect(server.messageCalls().filter((c) => c.includes(C2))).toHaveLength(2),
    );
  });

  it('send carries the conversationId, also on the optimistic row and on retransmit', async () => {
    await store.getState().open(P, C2);
    expect(store.getState().send(P, C2, 'hi')).toBe(true);
    expect(ws.framesOfType('send')).toEqual([
      { type: 'send', projectId: P, conversationId: C2, clientMessageId: 'c1', text: 'hi', attachments: [] },
    ]);
    expect(store.getState().chats[K2]!.messages.at(-1)).toMatchObject({ id: -1, conversationId: C2 });
    expect(ids(K1)).toEqual([1, 3]);
    ws.drop();
    ws.connect();
    expect(ws.framesOfType('send').at(-1)).toMatchObject({ conversationId: C2, clientMessageId: 'c1' });
  });

  it('a refused send (conversation_closed) marks the row of the conversation that sent it', async () => {
    await store.getState().open(P, C2);
    store.getState().send(P, C2, 'late');
    await store.getState().open(P, C1);
    ws.emit({ type: 'error', projectId: P, code: 'conversation_closed', message: 'Session is closed', clientMessageId: 'c1' });
    expect(store.getState().chats[K2]!.messages.at(-1)).toMatchObject({ status: 'error' });
    expect(store.getState().chats[K2]!.lastError).toBe('Session is closed');
    expect(store.getState().chats[K1]!.lastError).toBeNull();
  });

  it('tracks the project-level active run across conversations', async () => {
    ws.emit({ type: 'run-started', projectId: P, conversationId: C2, runId: 'r1' });
    expect(store.getState().activeRuns[P]).toEqual({ conversationId: C2, runId: 'r1' });
    // the conversation shown is not the running one
    expect(store.getState().chats[K1]).toMatchObject({ runActive: false, currentRunId: null });
    ws.emit({ type: 'run-finished', projectId: P, conversationId: C1, runId: 'r0', reason: 'completed' });
    expect(store.getState().activeRuns[P]).toEqual({ conversationId: C2, runId: 'r1' }); // late finish of another run
    ws.emit({ type: 'run-finished', projectId: P, conversationId: C2, runId: 'r1', reason: 'completed' });
    expect(store.getState().activeRuns[P]).toBeUndefined();
  });

  it('run state of a loaded conversation follows its own frames only', async () => {
    await store.getState().open(P, C2);
    ws.emit({ type: 'run-started', projectId: P, conversationId: C2, runId: 'r1' });
    expect(store.getState().chats[K2]).toMatchObject({ runActive: true, currentRunId: 'r1' });
    expect(store.getState().chats[K1]).toMatchObject({ runActive: false });
  });

  it('stop interrupts the running run with its conversationId; without a run, the own conversation', () => {
    ws.emit({ type: 'run-started', projectId: P, conversationId: C2, runId: 'r1' });
    store.getState().stop(P, C1);
    ws.emit({ type: 'run-finished', projectId: P, conversationId: C2, runId: 'r1', reason: 'interrupted' });
    store.getState().stop(P, C1);
    expect(ws.framesOfType('interrupt')).toEqual([
      { type: 'interrupt', projectId: P, conversationId: C2, runId: 'r1' },
      { type: 'interrupt', projectId: P, conversationId: C1, runId: null },
    ]);
  });

  it('a lost socket clears the active run (the server re-sends run-started on subscribe)', () => {
    ws.emit({ type: 'run-started', projectId: P, conversationId: C1, runId: 'r1' });
    ws.drop();
    expect(store.getState().activeRuns).toEqual({});
    expect(store.getState().chats[K1]).toMatchObject({ runActive: false, currentRunId: null });
  });

  it('reset drops chats, active runs and the current conversations', () => {
    ws.emit({ type: 'run-started', projectId: P, conversationId: C1, runId: 'r1' });
    store.getState().reset();
    expect(store.getState()).toMatchObject({ chats: {}, activeRuns: {}, current: {} });
  });
});
