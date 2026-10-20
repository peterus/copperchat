import { act, render, renderHook, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const http = vi.hoisted(() => ({ apiGet: vi.fn(), apiPut: vi.fn(), apiPost: vi.fn() }));
vi.mock('@/api/http', async (importOriginal) => ({ ...(await importOriginal<object>()), ...http }));

import type { DocRead } from '@/api/docs';
import { usePanels } from '@/layouts/panels';
import { PHONE_QUERY, ProjectLayout } from '@/layouts/ProjectLayout';
import { createDocsStore, DocsStoreContext, draftKey } from '@/stores/docs';
import { useUi } from '@/stores/ui';
import { conflict409, DOC_PID, docEntry, docRead, REQ, revision } from '@/test/docs';
import { FakeWsClient } from '@/test/fakeWsClient';
import { setMedia } from '@/test/media';
import { DocsPanel } from './DocsPanel';
import './docs';

const INDEX = {
  root: 'docs/design',
  docs: [docEntry(REQ), docEntry('docs/design/parts.md')],
  ignored: [],
};
const REV = { content: '# R\n\nnew\n', prevContent: '# R\n\nold\n' };
let current: DocRead;

function api(path: string): unknown {
  if (path.endsWith('/docs')) return INDEX;
  if (path.includes('/revisions?')) return [revision(2), revision(1)];
  if (/\/revisions\/\d+$/.test(path)) return REV;
  if (path.endsWith('/parts.md')) return docRead({ path: 'docs/design/parts.md', body: '# Parts\n' });
  return current;
}

function withStore(ui: React.ReactNode, store = createDocsStore(new FakeWsClient())) {
  return <DocsStoreContext.Provider value={store}>{ui}</DocsStoreContext.Provider>;
}

beforeEach(() => {
  vi.resetAllMocks();
  current = docRead();
  http.apiGet.mockImplementation(async (p: string) => api(p));
  http.apiPut.mockResolvedValue({ version: 'v2' });
  http.apiPost.mockResolvedValue({ version: 'v2' });
  useUi.setState({ panelRequest: null });
});

describe('DocsPanel', () => {
  it('is registered as a wide panel with order 12', () => {
    const { result } = renderHook(() => usePanels());
    expect(result.current.find((p) => p.id === 'docs')).toMatchObject({ title: 'Docs', order: 12, wide: true });
  });

  it('no params: the index; a doc opens; the back arrow returns', async () => {
    render(withStore(<DocsPanel projectId={DOC_PID} />));
    await userEvent.click(await screen.findByText('requirements.md'));
    expect(await screen.findByRole('heading', { name: 'Requirements' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: '← Docs' }));
    expect(await screen.findByRole('list', { name: 'Design docs' })).toBeInTheDocument();
  });

  it('{path} opens that doc', async () => {
    render(withStore(<DocsPanel projectId={DOC_PID} params={{ path: 'docs/design/parts.md' }} />));
    expect(await screen.findByRole('heading', { name: 'Parts' })).toBeInTheDocument();
  });

  it('{path, revisionId} opens its history diff; back goes to the doc', async () => {
    render(withStore(<DocsPanel projectId={DOC_PID} params={{ path: REQ, revisionId: '2' }} />));
    await waitFor(() => expect(screen.getAllByRole('row').map((r) => r.textContent)).toContain('+ new'));
    expect(http.apiGet).toHaveBeenCalledWith(`/projects/${DOC_PID}/docs/${REQ}/revisions/2`);
    await userEvent.click(screen.getByRole('button', { name: '← requirements.md' }));
    expect(await screen.findByRole('heading', { name: 'Requirements' })).toBeInTheDocument();
  });

  it('new params while open navigate there', async () => {
    const { rerender } = render(withStore(<DocsPanel projectId={DOC_PID} />));
    await screen.findByRole('list', { name: 'Design docs' });
    rerender(withStore(<DocsPanel projectId={DOC_PID} params={{ path: 'docs/design/parts.md' }} />));
    expect(await screen.findByRole('heading', { name: 'Parts' })).toBeInTheDocument();
  });

  it('after saving an approved doc the panel asks "Approve again?"', async () => {
    current = docRead({ meta: { approved: true, status: 'approved', approvedVersion: 'v1' } });
    render(withStore(<DocsPanel projectId={DOC_PID} params={{ path: REQ }} />));
    await userEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    current = docRead({ version: 'v2', meta: { approvedVersion: 'v1', changedSinceApproval: true } });
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText(/approve again\?/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Approve again' }));
    expect(http.apiPost).toHaveBeenCalledWith(`/projects/${DOC_PID}/docs/${REQ}/approve`, { version: 'v2' });
    await waitFor(() => expect(screen.queryByText(/approve again\?/)).toBeNull());
  });

  it('saving a draft doc does not ask to approve again', async () => {
    render(withStore(<DocsPanel projectId={DOC_PID} params={{ path: REQ }} />));
    await userEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByRole('button', { name: 'Edit' });
    expect(screen.queryByText(/approve again\?/)).toBeNull();
  });

  it('a 409 on revert continues in the editor conflict flow; Discard mine returns to the doc', async () => {
    http.apiPost.mockRejectedValue(conflict409());
    render(withStore(<DocsPanel projectId={DOC_PID} params={{ path: REQ, revisionId: '2' }} />));
    await userEvent.click(await screen.findByRole('button', { name: 'Revert to this' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Changed while you were editing (by Session 3 — Schematic).');
    expect((screen.getByRole('textbox', { name: 'Doc text' }) as HTMLTextAreaElement).value).toBe('# R\n\nnew\n');
    expect(http.apiPost).toHaveBeenCalledWith(`/projects/${DOC_PID}/docs/${REQ}/revisions/2/revert`, { baseVersion: 'v1' });
    await userEvent.click(screen.getByRole('button', { name: 'Discard mine' }));
    expect(await screen.findByRole('heading', { name: 'Requirements' })).toBeInTheDocument();
  });

  it('phone: under More, full-screen, index → doc → history with back arrows', async () => {
    setMedia((q) => q === PHONE_QUERY);
    render(
      withStore(
        <MemoryRouter>
          <ProjectLayout projectId={DOC_PID} title="demo" viewer={<div>VIEWER</div>} chat={<div>CHAT</div>} />
        </MemoryRouter>,
      ),
    );
    await userEvent.click(screen.getByRole('tab', { name: 'More' }));
    await userEvent.click(screen.getByRole('button', { name: 'Docs' }));
    expect(screen.queryByRole('dialog')).toBeNull(); // not a side sheet
    expect(screen.getByText('CHAT').parentElement).not.toBeVisible();
    await userEvent.click(await screen.findByText('requirements.md'));
    await userEvent.click(await screen.findByRole('button', { name: 'History' }));
    await userEvent.click((await screen.findAllByRole('listitem'))[0]!.querySelector('button')!);
    await waitFor(() => expect(screen.getAllByRole('row').map((r) => r.textContent)).toContain('+ new'));
    await userEvent.click(screen.getByRole('button', { name: '← All revisions' }));
    await userEvent.click(screen.getByRole('button', { name: '← requirements.md' }));
    await userEvent.click(await screen.findByRole('button', { name: '← Docs' }));
    expect(await screen.findByRole('list', { name: 'Design docs' })).toBeInTheDocument();
    await userEvent.click(screen.getByText('requirements.md'));
    await userEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    expect(screen.getByRole('textbox', { name: 'Doc text' }).closest('[data-fullscreen="true"]')).not.toBeNull();
  });

  it("phone: openPanel('docs', {path, revisionId}) from a card opens the diff under More", async () => {
    setMedia((q) => q === PHONE_QUERY);
    render(
      withStore(
        <MemoryRouter>
          <ProjectLayout projectId={DOC_PID} title="demo" viewer={<div>VIEWER</div>} chat={<div>CHAT</div>} />
        </MemoryRouter>,
      ),
    );
    act(() => useUi.getState().openPanel('docs', { path: REQ, revisionId: '2' }));
    await waitFor(() => expect(screen.getAllByRole('row').map((r) => r.textContent)).toContain('+ new'));
    expect(within(screen.getByRole('tablist')).getByRole('tab', { name: 'More' })).toHaveAttribute('aria-selected', 'true');
  });

  it('[View diff] while editing keeps the text as a draft that resumes from the doc view', async () => {
    const store = createDocsStore(new FakeWsClient());
    const { rerender } = render(withStore(<DocsPanel projectId={DOC_PID} params={{ path: REQ }} />, store));
    await userEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    await userEvent.type(screen.getByRole('textbox', { name: 'Doc text' }), 'unsaved');
    rerender(withStore(<DocsPanel projectId={DOC_PID} params={{ path: REQ, revisionId: '2' }} />, store));
    await waitFor(() => expect(screen.getAllByRole('row').map((r) => r.textContent)).toContain('+ new'));
    await userEvent.click(screen.getByRole('button', { name: '← requirements.md' }));
    expect(await screen.findByText('You have unsaved changes to this doc.')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Continue editing' }));
    expect((screen.getByRole('textbox', { name: 'Doc text' }) as HTMLTextAreaElement).value).toMatch(/unsaved$/);
  });

  it('Discard draft (confirmed) drops the kept draft', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const store = createDocsStore(new FakeWsClient());
    store.getState().setDraft(DOC_PID, REQ, { text: 'x', base: { version: 'v1', body: '', approved: false }, conflict: null });
    render(withStore(<DocsPanel projectId={DOC_PID} params={{ path: REQ }} />, store));
    await userEvent.click(await screen.findByRole('button', { name: 'Discard draft' }));
    expect(store.getState().drafts).toEqual({});
    expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument();
  });

  it('the same [View diff] request twice navigates again (a new params object)', async () => {
    const store = createDocsStore(new FakeWsClient());
    const { rerender } = render(withStore(<DocsPanel projectId={DOC_PID} params={{ path: REQ, revisionId: '2' }} />, store));
    await userEvent.click(await screen.findByRole('button', { name: '← All revisions' }));
    await userEvent.click(screen.getByRole('button', { name: '← requirements.md' }));
    await screen.findByRole('button', { name: 'Edit' });
    rerender(withStore(<DocsPanel projectId={DOC_PID} params={{ path: REQ, revisionId: '2' }} />, store));
    expect(await screen.findByRole('button', { name: 'Revert to this' })).toBeInTheDocument();
  });

  it('closing the panel detaches the store', async () => {
    const store = createDocsStore(new FakeWsClient());
    const { unmount } = render(withStore(<DocsPanel projectId={DOC_PID} params={{ path: REQ }} />, store));
    await screen.findByRole('button', { name: 'Edit' });
    unmount();
    expect(store.getState().openPath).toBeNull();
  });

  it('a revert 409 asks before replacing an unsaved draft', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);
    http.apiPost.mockRejectedValue(conflict409());
    const store = createDocsStore(new FakeWsClient());
    store.getState().setDraft(DOC_PID, REQ, { text: 'my draft', base: { version: 'v1', body: '', approved: false }, conflict: null });
    render(withStore(<DocsPanel projectId={DOC_PID} params={{ path: REQ, revisionId: '2' }} />, store));
    await userEvent.click(await screen.findByRole('button', { name: 'Revert to this' }));
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole('textbox', { name: 'Doc text' })).toBeNull();
    expect(store.getState().drafts[draftKey(DOC_PID, REQ)]?.text).toBe('my draft');
    await userEvent.click(screen.getByRole('button', { name: 'Revert to this' }));
    expect(await screen.findByRole('textbox', { name: 'Doc text' })).toHaveValue('# R\n\nnew\n');
  });

  it('the index marks docs with an unsaved draft', async () => {
    const store = createDocsStore(new FakeWsClient());
    store.getState().setDraft(DOC_PID, REQ, { text: 'x', base: { version: 'v1', body: '', approved: false }, conflict: null });
    render(withStore(<DocsPanel projectId={DOC_PID} />, store));
    const row = (await screen.findByText('requirements.md')).closest('button')!;
    expect(within(row).getByText('unsaved draft')).toBeInTheDocument();
    expect(within(screen.getByText('parts.md').closest('button')!).queryByText('unsaved draft')).toBeNull();
  });
});
