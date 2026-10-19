import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CID, FakeMessageServer, PID, row } from '@/test/fakeMessageServer';
import { FakeWsClient } from '@/test/fakeWsClient';
import { chatKey, createChatStore, fetchMessagesPage, type ChatStore } from './chat';

const P = PID;
const C = CID;
const Q = '0123456789ab';
let ws: FakeWsClient;
let server: FakeMessageServer;
let store: ChatStore;
const chat = () => store.getState().chats[chatKey(P, C)]!;
const msg = (id: number) => chat().messages.find((m) => m.id === id);

beforeEach(async () => {
  ws = new FakeWsClient();
  ws.open = true;
  server = new FakeMessageServer([row(10, { role: 'user' }), row(11, { status: 'streaming', content: 'Hel' })]).install();
  store = createChatStore({ ws, fetchPage: fetchMessagesPage });
  await store.getState().open(P, C);
  ws.emit({ type: 'run-started', projectId: P, conversationId: C, runId: 'r1' });
});

describe('reconnect', () => {
  it('resets runActive (no stuck Stop) and resubscribes from lastSeenId', () => {
    expect(chat().runActive).toBe(true);
    ws.drop();
    expect(chat()).toMatchObject({ runActive: false, currentRunId: null });
    ws.connect();
    expect(chat()).toMatchObject({ runActive: false, currentRunId: null });
    expect(ws.framesOfType('subscribe').at(-1)).toEqual({ type: 'subscribe', projectId: P, conversationId: C, sinceMessageId: 11 });
  });

  it('a run-started re-sent by the server after resubscribe restores runActive', () => {
    ws.drop();
    ws.connect();
    ws.emit({ type: 'run-started', projectId: P, conversationId: C, runId: 'r1' });
    expect(chat()).toMatchObject({ runActive: true, currentRunId: 'r1' });
  });

  it('heals a half-streamed row via HTTP merge after reconnect', async () => {
    server.rows = [row(10, { role: 'user' }), row(11, { status: 'done', content: 'Hello world' })];
    ws.drop();
    ws.connect();
    await vi.waitFor(() => expect(msg(11)).toMatchObject({ content: 'Hello world', status: 'done' }));
    expect(chat().messages.map((m) => m.id)).toEqual([10, 11]);
    expect(chat().suspectIds).toEqual([]);
  });

  it('keeps longer live content when the DB snapshot lags behind the stream', async () => {
    ws.emit({ type: 'message-delta', projectId: P, conversationId: C, messageId: 11, contentDelta: 'lo wor' });
    server.rows = [row(10, { role: 'user' }), row(11, { status: 'streaming', content: 'Hello' })];
    ws.resume();
    await vi.waitFor(() => expect(server.messageCalls()).toHaveLength(3)); // baseline + its heal + this heal
    await vi.waitFor(() => expect(msg(11)?.content).toBe('Hello wor'));
    expect(chat().suspectIds).toEqual([11]);
  });

  it('re-heals a suspect row once it reaches a terminal status', async () => {
    ws.drop();
    ws.connect();
    await vi.waitFor(() => expect(server.messageCalls()).toHaveLength(3)); // baseline + its heal + this heal
    expect(chat().suspectIds).toEqual([11]);
    server.rows = [row(10, { role: 'user' }), row(11, { status: 'done', content: 'Hello, complete' })];
    ws.emit({ type: 'message-delta', projectId: P, conversationId: C, messageId: 11, contentDelta: 'p', statusPatch: 'done' });
    await vi.waitFor(() => expect(msg(11)?.content).toBe('Hello, complete'));
    expect(chat().suspectIds).toEqual([]);
  });

  it('a delta for an unknown message id triggers a heal instead of being dropped', async () => {
    server.rows = [...server.rows, row(12, { status: 'streaming', content: 'New row' })];
    ws.emit({ type: 'message-delta', projectId: P, conversationId: C, messageId: 12, contentDelta: ' row' });
    await vi.waitFor(() => expect(msg(12)?.content).toBe('New row'));
    expect(chat().lastSeenId).toBe(12);
  });

  it('visibility resume (socket still open) runs the same heal path', async () => {
    ws.resume();
    expect(chat().runActive).toBe(false);
    expect(ws.framesOfType('subscribe')).toHaveLength(2);
    await vi.waitFor(() => expect(server.messageCalls()).toHaveLength(3)); // baseline + its heal + this heal
  });

  it('a failing heal keeps state and logs a warning', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    server.failNext = 1;
    ws.resume();
    await vi.waitFor(() => expect(warn).toHaveBeenCalled());
    expect(chat().messages.map((m) => m.id)).toEqual([10, 11]);
  });

  it('concurrent heal requests coalesce into at most one extra fetch', async () => {
    server.hold();
    const a = store.getState().heal(P, C);
    const b = store.getState().heal(P, C);
    const c = store.getState().heal(P, C);
    server.release();
    await Promise.all([a, b, c]);
    server.release();
    await vi.waitFor(() => expect(server.messageCalls().length).toBeLessThanOrEqual(4));
    expect(server.messageCalls()).toHaveLength(4); // baseline + its heal + one heal + one coalesced re-run
  });

  it('a terminal refusal (unauthorized/forbidden) clears the run but neither resubscribes nor heals until open', async () => {
    for (const state of ['unauthorized', 'forbidden'] as const) {
      ws.emit({ type: 'run-started', projectId: P, conversationId: C, runId: 'r1' });
      const subscribes = ws.framesOfType('subscribe').length;
      const calls = server.messageCalls().length;
      ws.refuse(state);
      expect(chat()).toMatchObject({ runActive: false, currentRunId: null });
      await Promise.resolve();
      expect(ws.framesOfType('subscribe')).toHaveLength(subscribes);
      expect(server.messageCalls()).toHaveLength(calls);
      ws.connect();
      expect(ws.framesOfType('subscribe')).toHaveLength(subscribes + 1);
      await vi.waitFor(() => expect(server.messageCalls()).toHaveLength(calls + 1));
    }
  });

  it('resubscribes every loaded project on the shared socket', async () => {
    server.rows = [...server.rows, row(20, { projectId: Q })];
    await store.getState().open(Q, C);
    ws.drop();
    ws.connect();
    const last = ws.framesOfType('subscribe').slice(-2);
    expect(last).toEqual([
      { type: 'subscribe', projectId: P, conversationId: C, sinceMessageId: 11 },
      { type: 'subscribe', projectId: Q, conversationId: C, sinceMessageId: 20 },
    ]);
  });

  it('a not_cancellable error heals the optimistically cancelled row back to its server status', async () => {
    ws.emit({ type: 'message', projectId: P, message: row(12, { role: 'user', status: 'queued' }) });
    server.rows = [...server.rows, row(12, { role: 'user', status: 'done' })];
    store.getState().cancelQueued(P, C, 12);
    expect(msg(12)?.status).toBe('cancelled');
    ws.emit({ type: 'error', projectId: P, code: 'not_cancellable', message: 'message already started or finished' });
    await vi.waitFor(() => expect(msg(12)?.status).toBe('done'));
  });

  it('a baseline taken mid-stream marks the streaming row suspect and heals it when it settles', async () => {
    // The GET overlays live content; deltas between the GET and the subscribe are never replayed.
    expect(chat().suspectIds).toEqual([11]);
    server.rows = [row(10, { role: 'user' }), row(11, { status: 'done', content: 'Hello world' })];
    ws.emit({ type: 'message-delta', projectId: P, conversationId: C, messageId: 11, contentDelta: 'd', statusPatch: 'done' });
    await vi.waitFor(() => expect(msg(11)).toMatchObject({ content: 'Hello world', status: 'done' }));
    expect(chat().suspectIds).toEqual([]);
  });

  it('a heal that merges a still-streaming row makes it suspect (no duplicated text after it settles)', async () => {
    ws.emit({ type: 'message', projectId: P, message: row(12, { status: 'streaming', content: 'Hi' }) });
    expect(chat().suspectIds).not.toContain(12);
    // The DB is ahead of the client: the heal adopts it, then an in-flight delta duplicates text.
    server.rows = [...server.rows, row(12, { status: 'streaming', content: 'Hi there' })];
    await store.getState().heal(P, C);
    expect(chat().suspectIds).toContain(12);
    ws.emit({ type: 'message-delta', projectId: P, conversationId: C, messageId: 12, contentDelta: ' there' });
    expect(msg(12)?.content).toBe('Hi there there');
    server.rows = [...server.rows.filter((r) => r.id !== 12), row(12, { status: 'done', content: 'Hi there' })];
    ws.emit({ type: 'message-delta', projectId: P, conversationId: C, messageId: 12, statusPatch: 'done' });
    await vi.waitFor(() => expect(msg(12)).toMatchObject({ content: 'Hi there', status: 'done' }));
    expect(chat().suspectIds).not.toContain(12);
  });
});

describe('terminal delta lost between the baseline GET and the subscribe', () => {
  it('heals the baseline streaming row after subscribing (socket already open)', async () => {
    const other = createChatStore({
      ws,
      fetchPage: async (projectId, conversationId, opts) => {
        const page = await fetchMessagesPage(projectId, conversationId, opts);
        // The row finishes before we subscribe: its one terminal delta reaches nobody.
        server.rows = [row(10, { role: 'user' }), row(11, { status: 'done', content: 'Hello world' })];
        return page;
      },
    });
    await other.getState().open(P, C);
    expect(other.getState().chats[chatKey(P, C)]!.messages.find((m) => m.id === 11)).toMatchObject({ content: 'Hello world', status: 'done' });
    expect(other.getState().chats[chatKey(P, C)]!.suspectIds).toEqual([]);
  });

  it('a run-finished while rows are suspect heals them', async () => {
    expect(chat().suspectIds).toEqual([11]);
    server.rows = [row(10, { role: 'user' }), row(11, { status: 'done', content: 'Hello world' })];
    ws.emit({ type: 'run-finished', projectId: P, conversationId: C, runId: 'r1', reason: 'completed' });
    await vi.waitFor(() => expect(msg(11)).toMatchObject({ content: 'Hello world', status: 'done' }));
    expect(chat().suspectIds).toEqual([]);
  });
});

describe('heal keeps row identity', () => {
  it('an unchanged row keeps its object identity across a heal; a changed one is replaced', async () => {
    server.rows = [row(10, { role: 'user', metadata: { a: 1 } }), row(11, { status: 'streaming', content: 'Hel' })];
    await store.getState().heal(P, C);
    const before10 = msg(10);
    const before11 = msg(11);
    server.rows = [row(10, { role: 'user', metadata: { a: 1 } }), row(11, { status: 'done', content: 'Hello' })];
    await store.getState().heal(P, C);
    expect(msg(10)).toBe(before10);
    expect(msg(11)).not.toBe(before11);
    expect(msg(11)).toMatchObject({ status: 'done', content: 'Hello' });
  });
});

describe('failed initial load', () => {
  it('is retried when the connection opens or resumes', async () => {
    const other = createChatStore({ ws, fetchPage: fetchMessagesPage });
    server.failNext = 1;
    await other.getState().open(Q, C);
    expect(other.getState().chats[chatKey(Q, C)]).toMatchObject({ loaded: false, lastError: expect.stringContaining('Could not load') });
    ws.drop();
    ws.connect();
    await vi.waitFor(() => expect(other.getState().chats[chatKey(Q, C)]?.loaded).toBe(true));
    expect(other.getState().chats[chatKey(Q, C)]?.lastError).toBeNull();
    server.failNext = 1;
    other.getState().reset();
    await other.getState().open(Q, C);
    ws.resume();
    await vi.waitFor(() => expect(other.getState().chats[chatKey(Q, C)]?.loaded).toBe(true));
  });
});

describe('over-length / invalid sends', () => {
  it('rejects a send the protocol would refuse without inserting an optimistic row', () => {
    const before = chat().messages.length;
    expect(store.getState().send(P, C, 'x'.repeat(100_001))).toBe(false);
    expect(chat().messages).toHaveLength(before);
    expect(chat().lastError).toMatch(/not sent/i);
    expect(ws.framesOfType('send')).toEqual([]);
    const tooMany = Array.from({ length: 21 }, (_, i) => ({ id: `att${i}`, kind: 'image' as const, name: `a${i}.png` }));
    expect(store.getState().send(P, C, 'hi', tooMany)).toBe(false);
    expect(chat().messages).toHaveLength(before);
    expect(store.getState().send(P, C, 'fine')).toBe(true);
    expect(chat().messages).toHaveLength(before + 1);
  });

  it('a pending row that fails validation on retransmit is marked failed and does not break reconnect', () => {
    ws.open = false;
    expect(store.getState().send(P, C, 'ok')).toBe(true);
    // Corrupt the pending row (e.g. persisted by an older client) so its frame no longer validates.
    store.setState({
      chats: {
        ...store.getState().chats,
        [chatKey(P, C)]: { ...chat(), messages: chat().messages.map((m) => (m.id < 0 ? { ...m, content: 'x'.repeat(100_001) } : m)) },
      },
    });
    expect(() => ws.connect()).not.toThrow();
    expect(ws.framesOfType('subscribe').at(-1)).toMatchObject({ projectId: P });
    expect(chat().messages.find((m) => m.id < 0)).toMatchObject({ status: 'error' });
  });
});
