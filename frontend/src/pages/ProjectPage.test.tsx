import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  closeConversation,
  createConversation,
  listConversations,
  patchConversation,
  reopenConversation,
} from '@/api/conversations';
import { ChatStoreContext } from '@/hooks/useChat';
import { createChatStore, type ChatDeps, type ChatStore } from '@/stores/chat';
import { ConversationsStoreContext, createConversationsStore, type ConversationsStore } from '@/stores/conversations';
import { useComposer } from '@/stores/composer';
import { useProjects } from '@/stores/projects';
import { FakeWsClient } from '@/test/fakeWsClient';
import { ProjectPage } from './ProjectPage';

const A = 'a1b2c3d4e5f6';
const B = '0123456789ab';
const list = [
  { id: A, name: 'demo', relPath: 'demo/demo.kicad_pro', hasSch: true, hasPcb: true, locked: false },
  { id: B, name: 'other', relPath: 'other/other.kicad_pro', hasSch: true, hasPcb: false, locked: false },
];
const openResult = (id: string) => ({ id, readOnly: false, lockFiles: [], kicadVersion: '10.99.0', viewFiles: { sch: [], pcb: [] } });

const C1 = '11111111111111111111111111111111';
const C2 = '22222222222222222222222222222222';
const C3 = '33333333333333333333333333333333';
const conv = (id: string, seq: number, status = 'open') => ({
  id,
  seq,
  title: `Session ${seq}`,
  type: 'free',
  status,
  archived: false,
  createdAt: '2026-10-19T10:00:00.000Z',
  lastMessageAt: null,
  closedAt: status === 'closed' ? '2026-10-19T11:00:00.000Z' : null,
  usage: { inputTokens: 0, outputTokens: 0, costUsd: 0 },
});

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

let store: ChatStore;
let conversations$: ConversationsStore;
let failOpen = false;
/** Per project: GET /conversations answer (newest activity first). */
let conversations: Record<string, ReturnType<typeof conv>[]>;
let posts: Array<{ url: string; body: unknown }>;
let fetchPage: ReturnType<typeof vi.fn<ChatDeps['fetchPage']>>;
let navigate: (to: string) => void = () => {};

function Nav() {
  navigate = useNavigate();
  return null;
}

function Where() {
  return <span data-testid="location">{useLocation().pathname}</span>;
}

beforeEach(() => {
  vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} unobserve() {} takeRecords() { return []; } });
  failOpen = false;
  conversations = { [A]: [conv(C1, 1)], [B]: [conv(C3, 1)] };
  posts = [];
  useProjects.getState().reset();
  useComposer.getState().reset();
  fetchPage = vi.fn<ChatDeps['fetchPage']>(async () => ({ messages: [], hasMore: false }));
  const ws = new FakeWsClient();
  store = createChatStore({ ws, fetchPage });
  conversations$ = createConversationsStore({
    ws,
    api: {
      list: listConversations,
      create: createConversation,
      patch: patchConversation,
      close: closeConversation,
      reopen: reopenConversation,
    },
  });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/api/projects') return json(list);
      const c = /^\/api\/projects\/([0-9a-f]{12})\/conversations(\?archived=([01]))?$/.exec(url);
      if (c && init?.method === 'POST') {
        const body = JSON.parse(String(init.body)) as unknown;
        posts.push({ url, body });
        const created = conv(C2, 1);
        conversations[c[1] ?? ''] = [created];
        return json(created, 201);
      }
      if (c) return json((conversations[c[1] ?? ''] ?? []).filter((x) => x.archived === (c[3] === '1')));
      const m = /^\/api\/projects\/([0-9a-f]{12})\/open$/.exec(url);
      if (m) return failOpen ? json({ detail: 'KiCad is not available' }, 503) : json(openResult(m[1] ?? ''));
      return new Response(null, { status: 404 });
    }),
  );
});

function renderPage(path: string) {
  return render(
    <ConversationsStoreContext.Provider value={conversations$}>
      <ChatStoreContext.Provider value={store}>
        <MemoryRouter initialEntries={[path]}>
          <Nav />
          <Routes>
            <Route path="/p/:projectId" element={<ProjectPage />} />
            <Route path="/p/:projectId/s/:cid" element={<ProjectPage />} />
            <Route path="/projects" element={<p>projects page</p>} />
          </Routes>
          <Where />
        </MemoryRouter>
      </ChatStoreContext.Provider>
    </ConversationsStoreContext.Provider>,
  );
}

const at = () => screen.getByTestId('location').textContent;

describe('ProjectPage', () => {
  it('opens the project, then shows the viewer slot and the chat of the session', async () => {
    renderPage(`/p/${A}/s/${C1}`);
    expect(screen.getByRole('status')).toHaveTextContent('Opening project…');
    expect(await screen.findByRole('heading', { name: 'demo' })).toBeInTheDocument();
    expect(screen.getByTestId('viewer-slot')).toHaveAttribute('data-project-id', A);
    expect(await screen.findByRole('region', { name: 'Chat' })).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'Session 1' })).toBeInTheDocument();
    await waitFor(() => expect(store.getState().current[A]).toBe(C1));
    expect(conversations$.getState().viewing[A]).toBe(C1);
  });

  it('/p/X redirects to the newest open session', async () => {
    conversations[A] = [conv(C3, 3, 'closed'), conv(C2, 2), conv(C1, 1)];
    renderPage(`/p/${A}`);
    await waitFor(() => expect(at()).toBe(`/p/${A}/s/${C2}`));
    await waitFor(() => expect(store.getState().current[A]).toBe(C2));
    expect(fetchPage).toHaveBeenCalledWith(A, C2, expect.anything());
    expect(posts).toEqual([]);
  });

  it('an empty project shows "Start a session" (nothing is created by itself)', async () => {
    conversations[A] = [];
    renderPage(`/p/${A}`);
    fireEvent.click(await screen.findByRole('button', { name: 'Start a session' }));
    expect(await screen.findByRole('dialog', { name: 'New session' })).toBeInTheDocument();
    expect(posts).toEqual([]);
    expect(at()).toBe(`/p/${A}`);
  });

  it('only closed sessions: the empty state offers the earlier sessions', async () => {
    conversations[A] = [conv(C3, 3, 'closed')];
    renderPage(`/p/${A}`);
    fireEvent.click(await screen.findByRole('button', { name: 'Earlier sessions' }));
    expect(screen.getByRole('list', { name: 'Sessions' })).toHaveTextContent('Session 3');
  });

  it('a session of another project (or a malformed id) is "Session not found" with a way back', async () => {
    renderPage(`/p/${A}/s/${C3}`);
    expect(await screen.findByText('Session not found')).toBeInTheDocument();
    expect(fetchPage).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('link', { name: 'Back to the sessions' }));
    await waitFor(() => expect(at()).toBe(`/p/${A}/s/${C1}`));

    act(() => navigate(`/p/${A}/s/not-an-id`));
    expect(await screen.findByText('Session not found')).toBeInTheDocument();
  });

  it('an archived session is found through the archived list', async () => {
    conversations[A] = [conv(C1, 1), { ...conv(C2, 2), archived: true }];
    renderPage(`/p/${A}/s/${C2}`);
    expect(await screen.findByRole('heading', { name: 'Session 2' })).toBeInTheDocument();
  });

  it('a failed conversation list shows the error with Retry', async () => {
    const real = globalThis.fetch;
    let fail = true;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) =>
        fail && url.includes('/conversations') ? json({ detail: 'boom' }, 500) : real(url, init),
      ),
    );
    renderPage(`/p/${A}`);
    expect(await screen.findByText(/Could not load the sessions: boom/)).toBeInTheDocument();
    fail = false;
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('region', { name: 'Chat' })).toBeInTheDocument();
  });

  it('a failed open shows the error with Retry instead of hanging', async () => {
    failOpen = true;
    renderPage(`/p/${A}`);
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not open the project: KiCad is not available');
    failOpen = false;
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('heading', { name: 'demo' })).toBeInTheDocument();
  });

  it('keeps one draft per session: switching sessions or projects swaps the draft', async () => {
    conversations[A] = [conv(C2, 2), conv(C1, 1)];
    renderPage(`/p/${A}/s/${C1}`);
    const box = await screen.findByRole('textbox', { name: 'Message' });
    fireEvent.change(box, { target: { value: 'draft for 1' } });
    act(() => navigate(`/p/${A}/s/${C2}`));
    await screen.findByRole('heading', { name: 'Session 2' });
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveValue('');
    act(() => navigate(`/p/${B}`));
    expect(await screen.findByRole('heading', { name: 'other' })).toBeInTheDocument();
    await waitFor(() => expect(useComposer.getState().text).toBe(''));
    act(() => navigate(`/p/${A}/s/${C1}`));
    await screen.findByRole('heading', { name: 'Session 1' });
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveValue('draft for 1');
  });

  it('switching sessions does not carry the header state over (no rename of the wrong session)', async () => {
    conversations[A] = [conv(C2, 2), conv(C1, 1)];
    renderPage(`/p/${A}/s/${C1}`);
    await screen.findByRole('heading', { name: 'Session 1' });
    fireEvent.click(screen.getByRole('button', { name: 'Rename session' }));
    expect(screen.getByRole('textbox', { name: 'Session title' })).toHaveValue('Session 1');
    act(() => navigate(`/p/${A}/s/${C2}`));
    expect(await screen.findByRole('heading', { name: 'Session 2' })).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Session title' })).toBeNull();
  });

  it('a failed archived lookup offers Retry instead of "Session not found"', async () => {
    const real = globalThis.fetch;
    let fail = true;
    conversations[A] = [conv(C1, 1), { ...conv(C2, 2), archived: true }];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) =>
        fail && url.endsWith('archived=1') ? json({ detail: 'offline' }, 500) : real(url, init),
      ),
    );
    renderPage(`/p/${A}/s/${C2}`);
    expect(await screen.findByText(/Could not load the sessions/)).toBeInTheDocument();
    expect(screen.queryByText('Session not found')).toBeNull();
    fail = false;
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('heading', { name: 'Session 2' })).toBeInTheDocument();
  });

  it('no chat shown (empty state): text inserted from the viewer lands in no draft', async () => {
    conversations[A] = [conv(C1, 1)];
    renderPage(`/p/${A}/s/${C1}`);
    await screen.findByRole('heading', { name: 'Session 1' });
    act(() => navigate(`/p/${B}/s/${'f'.repeat(32)}`));
    expect(await screen.findByText('Session not found')).toBeInTheDocument();
    act(() => useComposer.getState().insertText('@R1'));
    act(() => navigate(`/p/${A}/s/${C1}`));
    await screen.findByRole('heading', { name: 'Session 1' });
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveValue('');
  });

  it('a failed archived lookup is tried again after navigating away and back', async () => {
    const real = globalThis.fetch;
    let fail = true;
    conversations[A] = [conv(C1, 1), { ...conv(C2, 2), archived: true }];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) =>
        fail && url.endsWith('archived=1') ? json({ detail: 'offline' }, 500) : real(url, init),
      ),
    );
    renderPage(`/p/${A}/s/${C2}`);
    expect(await screen.findByText(/Could not load the sessions/)).toBeInTheDocument();
    fail = false;
    act(() => navigate(`/p/${A}/s/${C1}`));
    await screen.findByRole('heading', { name: 'Session 1' });
    act(() => navigate(`/p/${A}/s/${C2}`));
    expect(await screen.findByRole('heading', { name: 'Session 2' })).toBeInTheDocument();
  });
});
