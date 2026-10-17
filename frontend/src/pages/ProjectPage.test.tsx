import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ChatStoreContext } from '@/hooks/useChat';
import { createChatStore, type ChatStore } from '@/stores/chat';
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

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

let store: ChatStore;
let failOpen = false;
let navigate: (to: string) => void = () => {};

function Nav() {
  navigate = useNavigate();
  return null;
}

beforeEach(() => {
  vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} unobserve() {} takeRecords() { return []; } });
  failOpen = false;
  useProjects.getState().reset();
  useComposer.getState().clear();
  store = createChatStore({ ws: new FakeWsClient(), fetchPage: async () => ({ messages: [], hasMore: false }) });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url === '/api/projects') return json(list);
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
    expect(screen.getByRole('region', { name: 'Chat' })).toBeInTheDocument();
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
