import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ConversationDTO } from '@/protocol/frames';
import { conv } from '@/test/conv';
import { PID, row } from '@/test/fakeMessageServer';
import { FakeWsClient } from '@/test/fakeWsClient';
import { byActivity, createConversationsStore, type ConversationsApi, type ConversationsStore } from './conversations';

const C1 = '11111111111111111111111111111111';
const C2 = '22222222222222222222222222222222';
const C3 = '33333333333333333333333333333333';

let ws: FakeWsClient;
let api: { [K in keyof ConversationsApi]: ReturnType<typeof vi.fn<ConversationsApi[K]>> };
let store: ConversationsStore;
const ids = () => (store.getState().byProject[PID] ?? []).map((c) => c.id);

beforeEach(() => {
  ws = new FakeWsClient();
  ws.open = true;
  api = {
    list: vi.fn<ConversationsApi['list']>(async (_pid, opts) =>
      opts?.archived ? [conv(C3, 3, { archived: true })] : [conv(C2, 2), conv(C1, 1)],
    ),
    create: vi.fn<ConversationsApi['create']>(async (_pid, body) => conv(C3, 3, { type: body.type, title: body.title ?? 'Session 3' })),
    patch: vi.fn<ConversationsApi['patch']>(async (_pid, cid, body) => ({ ...conv(cid, cid === C1 ? 1 : 2), ...body })),
    close: vi.fn<ConversationsApi['close']>(async (_pid, cid) => conv(cid, 1, { status: 'closing' })),
    reopen: vi.fn<ConversationsApi['reopen']>(async (_pid, cid) => conv(cid, 1, { status: 'open' })),
  };
  store = createConversationsStore({ ws, api });
});

describe('byActivity', () => {
  it('sorts newest activity first (last message, else creation), then by seq', () => {
    const a = conv(C1, 1, { lastMessageAt: '2026-10-19T12:00:00.000Z' });
    const b = conv(C2, 2);
    const c = conv(C3, 3, { createdAt: b.createdAt });
    expect([b, c, a].sort(byActivity).map((x) => x.id)).toEqual([C1, C3, C2]);
  });
});

describe('conversations store', () => {
  it('loads a project list once (concurrent loads share the request)', async () => {
    await Promise.all([store.getState().load(PID), store.getState().load(PID)]);
    expect(api.list).toHaveBeenCalledTimes(1);
    expect(ids()).toEqual([C2, C1]);
    expect(store.getState().status[PID]).toBe('loaded');
  });

  it('a failed load records the error and can be retried', async () => {
    api.list.mockRejectedValueOnce(new Error('boom'));
    await store.getState().load(PID);
    expect(store.getState().status[PID]).toBe('error');
    expect(store.getState().errors[PID]).toBe('boom');
    await store.getState().load(PID);
    expect(ids()).toEqual([C2, C1]);
    expect(store.getState().errors[PID]).toBeNull();
  });

  it('conversation-updated upserts and re-sorts; other projects are ignored', async () => {
    await store.getState().load(PID);
    ws.emit({ type: 'conversation-updated', projectId: PID, conversation: conv(C1, 1, { lastMessageAt: '2026-10-19T12:00:00.000Z' }) });
    expect(ids()).toEqual([C1, C2]);
    ws.emit({ type: 'conversation-updated', projectId: PID, conversation: conv(C3, 3, { lastMessageAt: '2026-10-19T13:00:00.000Z' }) });
    expect(ids()).toEqual([C3, C1, C2]);
    ws.emit({ type: 'conversation-updated', projectId: 'ffffffffffff', conversation: conv(C3, 9) });
    expect(store.getState().byProject.ffffffffffff).toBeUndefined();
  });

  it('run-started / run-finished set and clear activeRun of the project', async () => {
    await store.getState().load(PID);
    ws.emit({ type: 'run-started', projectId: PID, conversationId: C1, runId: 'r1' });
    expect(store.getState().activeRun[PID]).toEqual({ conversationId: C1, runId: 'r1' });
    ws.emit({ type: 'run-finished', projectId: PID, conversationId: C1, runId: 'old', reason: 'completed' });
    expect(store.getState().activeRun[PID]).toEqual({ conversationId: C1, runId: 'r1' });
    ws.emit({ type: 'run-finished', projectId: PID, conversationId: C1, runId: 'r1', reason: 'completed' });
    expect(store.getState().activeRun[PID]).toBeNull();
  });

  it('a reconnect forgets the active run (re-sent on subscribe) and reloads the lists', async () => {
    await store.getState().load(PID);
    store.getState().view(PID, C1);
    ws.emit({ type: 'run-started', projectId: PID, conversationId: C1, runId: 'r1' });
    ws.drop();
    ws.connect();
    expect(store.getState().activeRun[PID]).toBeNull();
    await vi.waitFor(() => expect(api.list).toHaveBeenCalledTimes(2));
  });

  it('marks unread when a message arrives for a conversation that is not open', async () => {
    await store.getState().load(PID);
    store.getState().view(PID, C1);
    ws.emit({ type: 'message', projectId: PID, message: row(5, { conversationId: C1 }) });
    ws.emit({ type: 'message', projectId: PID, message: row(6, { conversationId: C2 }) });
    expect(store.getState().unread[PID]).toEqual([C2]);
    store.getState().view(PID, C2);
    expect(store.getState().unread[PID]).toEqual([]);
  });

  it('create / rename / archive / close / reopen upsert the server answer', async () => {
    await store.getState().load(PID);
    const created = await store.getState().create(PID, { type: 'requirements', title: 'Reqs' });
    expect(api.create).toHaveBeenCalledWith(PID, { type: 'requirements', title: 'Reqs' });
    expect(created.id).toBe(C3);
    expect(ids()).toContain(C3);
    await store.getState().rename(PID, C1, 'Power');
    expect(store.getState().byProject[PID]!.find((c) => c.id === C1)!.title).toBe('Power');
    await store.getState().setArchived(PID, C2, true);
    expect(api.patch).toHaveBeenCalledWith(PID, C2, { archived: true });
    expect(store.getState().byProject[PID]!.find((c) => c.id === C2)!.archived).toBe(true);
    await store.getState().close(PID, C1);
    expect(store.getState().byProject[PID]!.find((c) => c.id === C1)!.status).toBe('closing');
    await store.getState().reopen(PID, C1);
    expect(store.getState().byProject[PID]!.find((c) => c.id === C1)!.status).toBe('open');
  });

  it('loadArchived merges the archived list once', async () => {
    await store.getState().load(PID);
    await store.getState().loadArchived(PID);
    await store.getState().loadArchived(PID);
    expect(api.list).toHaveBeenCalledWith(PID, { archived: true });
    expect(api.list).toHaveBeenCalledTimes(2);
    expect(ids()).toEqual([C3, C2, C1]);
  });

  it('interrupt stops the running session, not the one shown', async () => {
    await store.getState().load(PID);
    store.getState().view(PID, C2);
    store.getState().interrupt(PID);
    expect(ws.framesOfType('interrupt')).toEqual([]);
    ws.emit({ type: 'run-started', projectId: PID, conversationId: C1, runId: 'r1' });
    store.getState().interrupt(PID);
    expect(ws.framesOfType('interrupt')).toEqual([{ type: 'interrupt', projectId: PID, conversationId: C1, runId: 'r1' }]);
  });

  it('reset drops everything; results of in-flight loads are discarded', async () => {
    let resolve!: (v: ConversationDTO[]) => void;
    api.list.mockImplementationOnce(() => new Promise((r) => (resolve = r)));
    const p = store.getState().load(PID);
    store.getState().reset();
    resolve([conv(C1, 1)]);
    await p;
    expect(store.getState().byProject[PID]).toBeUndefined();
  });

  it('a list answer older than a conversation-updated frame does not overwrite it', async () => {
    let resolve!: (v: ConversationDTO[]) => void;
    await store.getState().load(PID);
    api.list.mockImplementationOnce(() => new Promise((r) => (resolve = r)));
    const p = store.getState().load(PID);
    ws.emit({ type: 'conversation-updated', projectId: PID, conversation: conv(C1, 1, { status: 'closed' }) });
    resolve([conv(C2, 2, { title: 'Renamed' }), conv(C1, 1, { status: 'closing' })]);
    await p;
    const byId = Object.fromEntries(store.getState().byProject[PID]!.map((c) => [c.id, c]));
    expect(byId[C1]!.status).toBe('closed');
    expect(byId[C2]!.title).toBe('Renamed');
  });

  it('resubscribed(): forgets the active run (re-sent on subscribe) and reloads the list', async () => {
    await store.getState().load(PID);
    ws.emit({ type: 'run-started', projectId: PID, conversationId: C1, runId: 'r1' });
    store.getState().resubscribed(PID);
    expect(store.getState().activeRun[PID]).toBeNull();
    await vi.waitFor(() => expect(api.list).toHaveBeenCalledTimes(2));
  });

  it('concurrent loadArchived calls share one request and report failure', async () => {
    await store.getState().load(PID);
    const [a, b] = await Promise.all([store.getState().loadArchived(PID), store.getState().loadArchived(PID)]);
    expect([a, b]).toEqual([true, true]);
    expect(api.list).toHaveBeenCalledTimes(2);
    store.getState().reset();
    api.list.mockRejectedValueOnce(new Error('offline'));
    expect(await store.getState().loadArchived(PID)).toBe(false);
    expect(await store.getState().loadArchived(PID)).toBe(true); // retried, not cached as done
  });

  it('a reconnect reloads only the projects that are shown', async () => {
    await store.getState().load(PID);
    await store.getState().load('ffffffffffff');
    store.getState().view(PID, C1);
    store.getState().view('ffffffffffff', null);
    store.getState().leave('ffffffffffff');
    api.list.mockClear();
    ws.drop();
    ws.connect();
    await vi.waitFor(() => expect(api.list).toHaveBeenCalledTimes(1));
    expect(api.list).toHaveBeenCalledWith(PID, {});
  });
});
