import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  closeConversation,
  createConversation,
  fetchConversationMessages,
  listConversations,
  patchConversation,
  reopenConversation,
} from './conversations';

const PID = 'a1b2c3d4e5f6';
const CID = '0123456789abcdef0123456789abcdef';
const conv = {
  id: CID,
  seq: 1,
  title: 'Session 1',
  type: 'free',
  status: 'open',
  archived: false,
  createdAt: '2026-10-19T10:00:00.000Z',
  lastMessageAt: null,
  closedAt: null,
  usage: { inputTokens: 0, outputTokens: 0, costUsd: 0 },
};

let calls: Array<{ url: string; method: string; body: unknown }>;
let reply: unknown;
let status: number;

beforeEach(() => {
  calls = [];
  reply = conv;
  status = 200;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, method: init.method ?? 'GET', body: init.body ? JSON.parse(String(init.body)) : undefined });
      return new Response(JSON.stringify(reply), { status, headers: { 'content-type': 'application/json' } });
    }),
  );
});

describe('conversations api', () => {
  it('lists the active or archived conversations', async () => {
    reply = [conv];
    await expect(listConversations(PID)).resolves.toEqual([conv]);
    await listConversations(PID, { archived: true });
    expect(calls.map((c) => c.url)).toEqual([
      `/api/projects/${PID}/conversations?archived=0`,
      `/api/projects/${PID}/conversations?archived=1`,
    ]);
  });

  it('creates, patches, closes and reopens', async () => {
    status = 201;
    await expect(createConversation(PID, { type: 'free' })).resolves.toEqual(conv);
    status = 200;
    await patchConversation(PID, CID, { title: 'Renamed' });
    status = 202;
    await closeConversation(PID, CID);
    status = 200;
    await reopenConversation(PID, CID);
    expect(calls).toEqual([
      { url: `/api/projects/${PID}/conversations`, method: 'POST', body: { type: 'free' } },
      { url: `/api/projects/${PID}/conversations/${CID}`, method: 'PATCH', body: { title: 'Renamed' } },
      { url: `/api/projects/${PID}/conversations/${CID}/close`, method: 'POST', body: {} },
      { url: `/api/projects/${PID}/conversations/${CID}/reopen`, method: 'POST', body: {} },
    ]);
  });

  it('rejects a malformed DTO', async () => {
    reply = { ...conv, type: 'chat' };
    await expect(createConversation(PID, { type: 'free' })).rejects.toThrow();
  });

  it('pages the messages of one conversation', async () => {
    reply = { messages: [], hasMore: false };
    await fetchConversationMessages(PID, CID, { limit: 50, before: 7 });
    expect(calls[0]?.url).toBe(`/api/projects/${PID}/conversations/${CID}/messages?limit=50&before=7`);
  });
});
