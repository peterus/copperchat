import { beforeEach, describe, expect, it } from 'vitest';
import { FakeMessageServer, PID, row } from '@/test/fakeMessageServer';
import { FakeWsClient } from '@/test/fakeWsClient';
import { createChatStore, fetchMessagesPage, type ChatStore } from './chat';

const P = PID;
const range = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, i) => from + i);
let ws: FakeWsClient;
let server: FakeMessageServer;
let store: ChatStore;
let cid = 0;

function ids() {
  return store.getState().chats[P]?.messages.map((m) => m.id) ?? [];
}

beforeEach(() => {
  cid = 0;
  ws = new FakeWsClient();
  ws.open = true;
  server = new FakeMessageServer(range(1, 120).map((id) => row(id))).install();
  store = createChatStore({ ws, fetchPage: fetchMessagesPage, newClientMessageId: () => `c${++cid}` });
});

describe('chat store: baseline then subscribe', () => {
  it('subscribes only after the HTTP baseline has set lastSeenId', async () => {
    server.hold();
    const opening = store.getState().open(P);
    await Promise.resolve();
    expect(ws.framesOfType('subscribe')).toEqual([]);
    server.release();
    await opening;
    expect(ws.framesOfType('subscribe')).toEqual([{ type: 'subscribe', projectId: P, sinceMessageId: 120 }]);
    expect(ids()).toEqual(range(71, 120));
    expect(store.getState().chats[P]?.hasMoreOlder).toBe(true);
    expect(ws.ensureCalls).toBeGreaterThan(0);
  });

  it('double mount shares one in-flight load and subscribes once', async () => {
    const a = store.getState().open(P);
    const b = store.getState().open(P);
    expect(b).toBe(a);
    await Promise.all([a, b]);
    await store.getState().open(P);
    expect(server.messageCalls()).toEqual([`/api/projects/${P}/messages?limit=50`]);
    expect(ws.framesOfType('subscribe')).toHaveLength(1);
  });

  it('opening an already-loaded chat still ensures the socket (it stays down after a logout until ensure)', async () => {
    await store.getState().open(P);
    const calls = ws.ensureCalls;
    await store.getState().open(P);
    expect(ws.ensureCalls).toBe(calls + 1);
    expect(server.messageCalls()).toHaveLength(1);
  });

  it('defers subscribe to the connection handler when the socket is not open yet', async () => {
    ws.open = false;
    await store.getState().open(P);
    expect(ws.framesOfType('subscribe')).toEqual([]);
    ws.connect();
    expect(ws.framesOfType('subscribe')).toEqual([{ type: 'subscribe', projectId: P, sinceMessageId: 120 }]);
  });

  it('a failed baseline load sets lastError and can be retried by opening again', async () => {
    server.failNext = 1;
    await store.getState().open(P);
    expect(store.getState().chats[P]).toMatchObject({ loaded: false });
    expect(store.getState().chats[P]?.lastError).toMatch(/Could not load messages/);
    expect(ws.framesOfType('subscribe')).toEqual([]);
    await store.getState().open(P);
    expect(ids()).toEqual(range(71, 120));
    expect(ws.framesOfType('subscribe')).toHaveLength(1);
  });
});

describe('chat store: ordering and duplicates', () => {
  beforeEach(async () => {
    await store.getState().open(P);
  });

  it('replay overlapping the baseline yields exactly one row per id in id order', () => {
    for (const id of [118, 119, 120, 121, 122]) ws.emit({ type: 'message', projectId: P, message: row(id) });
    ws.emit({ type: 'message', projectId: P, message: row(121) });
    expect(ids()).toEqual(range(71, 122));
    expect(store.getState().chats[P]?.lastSeenId).toBe(122);
  });

  it('out-of-order live frames are inserted in id order', () => {
    ws.emit({ type: 'message', projectId: P, message: row(125) });
    ws.emit({ type: 'message', projectId: P, message: row(123) });
    ws.emit({ type: 'message', projectId: P, message: row(124) });
    expect(ids().slice(-3)).toEqual([123, 124, 125]);
  });

  it('ignores frames for projects that were never opened', () => {
    const before = store.getState().chats;
    ws.emit({ type: 'message', projectId: 'ffffffffffff', message: row(1, { projectId: 'ffffffffffff' }) });
    expect(store.getState().chats).toBe(before);
  });

  it('replaces the optimistic row by the server row with the same clientMessageId, also on retransmit echo', () => {
    store.getState().send(P, 'hello');
    store.getState().send(P, 'second');
    expect(ids().slice(-2)).toEqual([-1, -2]);
    expect(ws.framesOfType('send')[0]).toMatchObject({ clientMessageId: 'c1', text: 'hello', attachments: [] });
    const echo = row(121, { role: 'user', clientMessageId: 'c1', content: 'hello', status: 'queued' });
    ws.emit({ type: 'message', projectId: P, message: echo });
    ws.emit({ type: 'message', projectId: P, message: echo });
    expect(ids().slice(-2)).toEqual([121, -2]);
    expect(store.getState().chats[P]?.messages.filter((m) => m.clientMessageId === 'c1')).toHaveLength(1);
  });

  it('retransmits pending optimistic sends with the same clientMessageId after reconnect', () => {
    store.getState().send(P, 'hello');
    ws.drop();
    ws.connect();
    expect(ws.framesOfType('send').map((f) => f.clientMessageId)).toEqual(['c1', 'c1']);
  });

  it('a send while disconnected goes out exactly once when the socket opens', () => {
    ws.drop();
    store.getState().send(P, 'offline');
    expect(ws.framesOfType('send')).toEqual([]);
    ws.connect();
    expect(ws.framesOfType('send').map((f) => f.clientMessageId)).toEqual(['c1']);
  });

  it('updates are immutable', () => {
    ws.emit({ type: 'message', projectId: P, message: row(121, { status: 'streaming', content: 'Hel' }) });
    const before = store.getState().chats[P]!;
    const rowBefore = before.messages.at(-1)!;
    ws.emit({ type: 'message-delta', projectId: P, messageId: 121, contentDelta: 'lo' });
    const after = store.getState().chats[P]!;
    expect(after).not.toBe(before);
    expect(after.messages).not.toBe(before.messages);
    expect(rowBefore.content).toBe('Hel');
    expect(after.messages.at(-1)!.content).toBe('Hello');
  });

  it('a late run-finished for an older run does not clear the current run', () => {
    ws.emit({ type: 'run-started', projectId: P, runId: 'r2' });
    ws.emit({ type: 'run-finished', projectId: P, runId: 'r1', reason: 'completed' });
    expect(store.getState().chats[P]).toMatchObject({ runActive: true, currentRunId: 'r2' });
    ws.emit({ type: 'run-finished', projectId: P, runId: 'r2', reason: 'completed' });
    expect(store.getState().chats[P]).toMatchObject({ runActive: false, currentRunId: null });
  });

  it('error frame with clientMessageId marks the optimistic row as failed', () => {
    store.getState().send(P, 'x');
    ws.emit({ type: 'error', projectId: P, code: 'unknown_attachment', message: 'Unknown attachment', clientMessageId: 'c1' });
    const last = store.getState().chats[P]!.messages.at(-1)!;
    expect(last.status).toBe('error');
    expect(store.getState().chats[P]!.lastError).toBe('Unknown attachment');
    ws.drop();
    ws.connect();
    expect(ws.framesOfType('send')).toHaveLength(1); // failed rows are not retransmitted
  });

  it('stop sends interrupt with the current run id; cancelQueued sends cancel-queued', () => {
    ws.emit({ type: 'run-started', projectId: P, runId: 'r9' });
    store.getState().stop(P);
    expect(ws.framesOfType('interrupt')).toEqual([{ type: 'interrupt', projectId: P, runId: 'r9' }]);
    ws.emit({ type: 'message', projectId: P, message: row(130, { role: 'user', status: 'queued' }) });
    store.getState().cancelQueued(P, 130);
    expect(ws.framesOfType('cancel-queued')).toEqual([{ type: 'cancel-queued', projectId: P, messageId: 130 }]);
    expect(store.getState().chats[P]!.messages.at(-1)!.status).toBe('cancelled');
  });
});

describe('chat store: older pages', () => {
  beforeEach(async () => {
    await store.getState().open(P);
  });

  it('loads older pages with before=oldestId and prepends without duplicates', async () => {
    await store.getState().loadOlder(P);
    expect(server.messageCalls().at(-1)).toBe(`/api/projects/${P}/messages?limit=50&before=71`);
    expect(ids()).toEqual(range(21, 120));
    await store.getState().loadOlder(P);
    expect(ids()).toEqual(range(1, 120));
    expect(store.getState().chats[P]?.hasMoreOlder).toBe(false);
    await store.getState().loadOlder(P);
    expect(server.messageCalls()).toHaveLength(3);
  });

  it('concurrent loadOlder calls fetch the page once', async () => {
    await Promise.all([store.getState().loadOlder(P), store.getState().loadOlder(P)]);
    expect(server.messageCalls()).toHaveLength(2);
    expect(ids()).toEqual(range(21, 120));
  });

  it('a failed older-page load resets loadingOlder so it can be retried', async () => {
    server.failNext = 1;
    await store.getState().loadOlder(P);
    expect(store.getState().chats[P]).toMatchObject({ loadingOlder: false, hasMoreOlder: true });
    expect(store.getState().chats[P]?.lastError).toMatch(/older messages/);
    await store.getState().loadOlder(P);
    expect(ids()[0]).toBe(21);
  });
});
