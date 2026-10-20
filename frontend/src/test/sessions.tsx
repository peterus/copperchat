import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { vi } from 'vitest';
import type { ConversationDTO } from '@/protocol/frames';
import {
  ConversationsStoreContext,
  createConversationsStore,
  type ConversationsApi,
  type ConversationsStore,
} from '@/stores/conversations';
import { FakeWsClient } from './fakeWsClient';

export type FakeConversationsApi = { [K in keyof ConversationsApi]: ReturnType<typeof vi.fn<ConversationsApi[K]>> };

/** A conversations store over an in-memory list (mutated by the fake API like the server would). */
export function fakeConversations(initial: ConversationDTO[]) {
  const ws = new FakeWsClient();
  ws.open = true;
  const rows = [...initial];
  const update = (cid: string, patch: Partial<ConversationDTO>) => {
    const i = rows.findIndex((c) => c.id === cid);
    rows[i] = { ...rows[i]!, ...patch };
    return rows[i]!;
  };
  const api: FakeConversationsApi = {
    list: vi.fn<ConversationsApi['list']>(async (_pid, opts) => rows.filter((c) => c.archived === (opts?.archived ?? false))),
    create: vi.fn<ConversationsApi['create']>(async (_pid, body) => {
      const seq = rows.length + 1;
      const c: ConversationDTO = {
        id: seq.toString(16).padStart(32, 'c'),
        seq,
        title: body.title ?? `Session ${seq}`,
        type: body.type,
        status: 'open',
        archived: false,
        createdAt: '2026-10-20T09:00:00.000Z',
        lastMessageAt: null,
        closedAt: null,
        usage: { inputTokens: 0, outputTokens: 0, costUsd: 0 },
      };
      rows.push(c);
      return c;
    }),
    patch: vi.fn<ConversationsApi['patch']>(async (_pid, cid, body) => update(cid, body)),
    close: vi.fn<ConversationsApi['close']>(async (_pid, cid) => update(cid, { status: 'closing' })),
    reopen: vi.fn<ConversationsApi['reopen']>(async (_pid, cid) => update(cid, { status: 'open', closedAt: null })),
  };
  const store: ConversationsStore = createConversationsStore({ ws, api });
  return { ws, api, store, rows };
}

function Location() {
  const loc = useLocation();
  return <span data-testid="location">{loc.pathname}</span>;
}

/** Renders `ui` under a router at `path` with the given conversations store; the location is shown. */
export function renderWithSessions(ui: ReactNode, store: ConversationsStore, path = '/') {
  return render(
    <ConversationsStoreContext.Provider value={store}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="*" element={ui} />
        </Routes>
        <Location />
      </MemoryRouter>
    </ConversationsStoreContext.Provider>,
  );
}
