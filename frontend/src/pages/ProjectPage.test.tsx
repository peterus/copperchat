import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ChatStoreContext } from '@/hooks/useChat';
import { createChatStore, type ChatDeps, type ChatStore } from '@/stores/chat';
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

beforeEach(() => {
  vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} unobserve() {} takeRecords() { return []; } });
  failOpen = false;
  conversations = { [A]: [conv(C1, 1)], [B]: [conv(C3, 1)] };
  posts = [];
  useProjects.getState().reset();
  useComposer.getState().clear();
  fetchPage = vi.fn<ChatDeps['fetchPage']>(async () => ({ messages: [], hasMore: false }));
  store = createChatStore({ ws: new FakeWsClient(), fetchPage });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/api/projects') return json(list);
      const c = /^\/api\/projects\/([0-9a-f]{12})\/conversations(\?archived=0)?$/.exec(url);
      if (c && init?.method === 'POST') {
        const body = JSON.parse(String(init.body)) as unknown;
        posts.push({ url, body });
        const created = conv(C2, 1);
        conversations[c[1] ?? ''] = [created];
        return json(created, 201);
      }
      if (c) return json(conversations[c[1] ?? ''] ?? []);
      const m = /^\/api\/projects\/([0-9a-f]{12})\/open$/.exec(url);
      if (m) return failOpen ? json({ detail: 'KiCad is not available' }, 503) : json(openResult(m[1] ?? ''));
      return new Response(null, { status: 404 });
    }),
  );
});

function renderPage(path: string) {
  return render(
    <ChatStoreContext.Provider value={store}>
      <MemoryRouter initialEntries={[path]}>
        <Nav />
        <Routes>
          <Route path="/p/:projectId" element={<ProjectPage />} />
          <Route path="/projects" element={<p>projects page</p>} />
        </Routes>
      </MemoryRouter>
    </ChatStoreContext.Provider>,
  );
}

describe('ProjectPage', () => {
  it('opens the project, then shows the viewer slot and the chat', async () => {
    renderPage(`/p/${A}`);
    expect(screen.getByRole('status')).toHaveTextContent('Opening project…');
    expect(await screen.findByRole('heading', { name: 'demo' })).toBeInTheDocument();
    expect(screen.getByTestId('viewer-slot')).toHaveAttribute('data-project-id', A);
    expect(await screen.findByRole('region', { name: 'Chat' })).toBeInTheDocument();
  });

  it('lists the conversations and opens the newest open one (interim default session)', async () => {
    conversations[A] = [conv(C3, 3, 'closed'), conv(C2, 2), conv(C1, 1)];
    renderPage(`/p/${A}`);
    expect(await screen.findByRole('region', { name: 'Chat' })).toBeInTheDocument();
    await waitFor(() => expect(store.getState().current[A]).toBe(C2));
    expect(fetchPage).toHaveBeenCalledWith(A, C2, expect.anything());
    expect(posts).toEqual([]);
  });

  it('creates a free conversation when the project has none', async () => {
    conversations[A] = [];
    renderPage(`/p/${A}`);
    expect(await screen.findByRole('region', { name: 'Chat' })).toBeInTheDocument();
    expect(posts).toEqual([{ url: `/api/projects/${A}/conversations`, body: { type: 'free' } }]);
    await waitFor(() => expect(store.getState().current[A]).toBe(C2));
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
    expect(await screen.findByText(/Could not load the session: boom/)).toBeInTheDocument();
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

  it('switching projects clears the composer draft', async () => {
    renderPage(`/p/${A}`);
    await screen.findByRole('heading', { name: 'demo' });
    act(() => useComposer.getState().setText('draft for demo'));
    act(() => navigate(`/p/${B}`));
    expect(await screen.findByRole('heading', { name: 'other' })).toBeInTheDocument();
    await waitFor(() => expect(useComposer.getState().text).toBe(''));
  });
});
