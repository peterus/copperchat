import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const http = vi.hoisted(() => ({ apiGet: vi.fn(), apiPut: vi.fn() }));
vi.mock('@/api/http', async (importOriginal) => ({ ...(await importOriginal<object>()), ...http }));

import { ApiError } from '@/api/http';
import { PHONE_QUERY } from '@/layouts/ProjectLayout';
import { createDocsStore, DocsStoreContext, draftKey } from '@/stores/docs';
import { conflict409, DOC_PID, docRead, REQ } from '@/test/docs';
import { FakeWsClient } from '@/test/fakeWsClient';
import { setMedia } from '@/test/media';
import { DocEditor, type DocEditorProps } from './DocEditor';

const url = `/projects/${DOC_PID}/docs/${REQ}`;
const THEIRS = docRead({ version: 'v2', body: '# Requirements\n\nUSB-C power, 15 W.\n' });

async function setup(over: Partial<DocEditorProps> = {}, existing?: ReturnType<typeof createDocsStore>) {
  const ws = new FakeWsClient();
  const store =
    existing ??
    createDocsStore(ws, { getIndex: vi.fn().mockResolvedValue({ root: 'docs/design', docs: [], ignored: [] }), getDoc: vi.fn() });
  await store.getState().load(DOC_PID);
  const props: DocEditorProps = { projectId: DOC_PID, doc: docRead(), onCancel: vi.fn(), onSaved: vi.fn(), ...over };
  const view = render(
    <DocsStoreContext.Provider value={store}>
      <DocEditor {...props} />
    </DocsStoreContext.Provider>,
  );
  return { props, ws, store, view, box: () => screen.getByRole('textbox', { name: 'Doc text' }) as HTMLTextAreaElement };
}

beforeEach(() => {
  vi.resetAllMocks();
  http.apiPut.mockResolvedValue({ version: 'v9' });
  http.apiGet.mockResolvedValue(THEIRS);
});

describe('DocEditor', () => {
  it('edits the body (no front matter) with a preview toggle', async () => {
    const { box } = await setup();
    expect(box().value).toBe('# Requirements\n\nUSB-C power.\n');
    expect(box().value).not.toMatch(/^---/);
    await userEvent.click(screen.getByRole('button', { name: 'Preview' }));
    expect(screen.getByRole('heading', { name: 'Requirements' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Edit text' }));
    expect(box()).toBeInTheDocument();
  });

  it('Save sends PUT with the base version', async () => {
    const { props, box } = await setup();
    await userEvent.type(box(), 'More.');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(http.apiPut).toHaveBeenCalledWith(url, { body: '# Requirements\n\nUSB-C power.\nMore.', baseVersion: 'v1' });
    await waitFor(() => expect(props.onSaved).toHaveBeenCalledWith('v9', false));
  });

  it('saving an approved doc reports it, so the panel asks to approve again', async () => {
    const { props } = await setup({ doc: docRead({ meta: { approved: true, status: 'approved', approvedVersion: 'v1' } }) });
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(props.onSaved).toHaveBeenCalledWith('v9', true));
  });

  it('a missing doc starts from its template and is created with baseVersion null', async () => {
    const { box } = await setup({ doc: docRead({ exists: false, version: null, body: '', template: '# T\n' }) });
    expect(box().value).toBe('# T\n');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(http.apiPut).toHaveBeenCalledWith(url, { body: '# T\n', baseVersion: null });
  });

  describe('409 conflict', () => {
    async function conflicted() {
      http.apiPut.mockRejectedValueOnce(conflict409());
      const s = await setup();
      await userEvent.clear(s.box());
      await userEvent.type(s.box(), 'mine');
      await userEvent.click(screen.getByRole('button', { name: 'Save' }));
      expect(await screen.findByRole('alert')).toHaveTextContent('Changed while you were editing (by Session 3 — Schematic).');
      return s;
    }

    it('keeps the draft and blocks Save until a choice', async () => {
      const { props, box } = await conflicted();
      expect(box().value).toBe('mine');
      expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
      expect(props.onSaved).not.toHaveBeenCalled();
    });

    it('Show their change: a diff from base to current', async () => {
      await conflicted();
      await userEvent.click(screen.getByRole('button', { name: 'Show their change' }));
      const diff = await screen.findByRole('region', { name: 'Their change' });
      expect(http.apiGet).toHaveBeenCalledWith(url);
      const rows = [...diff.querySelectorAll('[role=row]')].map((r) => r.textContent);
      expect(rows).toContain('- USB-C power.');
      expect(rows).toContain('+ USB-C power, 15 W.');
    });

    it('Keep editing on top of current: after a confirm, base = current, text kept', async () => {
      const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);
      const { props, box } = await conflicted();
      await userEvent.click(screen.getByRole('button', { name: 'Keep editing on top of current' }));
      expect(http.apiGet).not.toHaveBeenCalled();
      expect(screen.getByRole('alert')).toBeInTheDocument();
      await userEvent.click(screen.getByRole('button', { name: 'Keep editing on top of current' }));
      await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
      expect(box().value).toBe('mine');
      await userEvent.click(screen.getByRole('button', { name: 'Save' }));
      expect(http.apiPut).toHaveBeenLastCalledWith(url, { body: 'mine', baseVersion: 'v2' });
      await waitFor(() => expect(props.onSaved).toHaveBeenCalledWith('v9', false));
      expect(confirm).toHaveBeenCalledTimes(2);
    });

    it('Keep editing on top of a deleted doc re-creates it (baseVersion null)', async () => {
      vi.spyOn(window, 'confirm').mockReturnValue(true);
      http.apiGet.mockResolvedValue(docRead({ exists: false, version: null, body: '' }));
      await conflicted();
      await userEvent.click(screen.getByRole('button', { name: 'Keep editing on top of current' }));
      await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
      await userEvent.click(screen.getByRole('button', { name: 'Save' }));
      expect(http.apiPut).toHaveBeenLastCalledWith(url, { body: 'mine', baseVersion: null });
    });

    it('Discard mine leaves the editor', async () => {
      const { props } = await conflicted();
      await userEvent.click(screen.getByRole('button', { name: 'Discard mine' }));
      expect(props.onCancel).toHaveBeenCalled();
      expect(http.apiPut).toHaveBeenCalledTimes(1);
    });

    it('a second 409 after re-basing goes through the flow again', async () => {
      vi.spyOn(window, 'confirm').mockReturnValue(true);
      const { box } = await conflicted();
      await userEvent.click(screen.getByRole('button', { name: 'Keep editing on top of current' }));
      await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
      http.apiPut.mockRejectedValueOnce(conflict409({ currentVersion: 'v3', conversationTitle: null, author: 'user' }));
      await userEvent.click(screen.getByRole('button', { name: 'Save' }));
      expect(await screen.findByRole('alert')).toHaveTextContent('(by you, in another window)');
      expect(box().value).toBe('mine');
    });
  });

  it('a seeded conflict (revert) starts in the conflict flow with the seeded text', async () => {
    const { box } = await setup({
      seed: { draft: 'old text', conflict: { currentVersion: 'v2', author: 'agent', conversationTitle: null } },
    });
    expect(box().value).toBe('old text');
    expect(screen.getByRole('alert')).toHaveTextContent('(by the agent)');
  });

  it('another error keeps the draft and shows the message', async () => {
    http.apiPut.mockRejectedValueOnce(
      new ApiError(413, { error: { code: 'doc_too_large', message: 'body is too large: the limit is 256 KiB' } }, 'body is too large: the limit is 256 KiB'),
    );
    const { props, box } = await setup();
    await userEvent.type(box(), 'x');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('256 KiB');
    expect(box().value).toMatch(/x$/);
    expect(props.onSaved).not.toHaveBeenCalled();
  });

  it('a doc-updated frame while editing shows the banner and keeps the text', async () => {
    const { ws, box } = await setup();
    await userEvent.type(box(), 'typing');
    // approve/unapprove frames keep the version: no banner
    act(() =>
      ws.emit({ type: 'doc-updated', projectId: DOC_PID, path: REQ, version: 'v1', status: 'approved', userEdited: false, revisionId: null, author: 'user' }),
    );
    expect(screen.queryByRole('status')).toBeNull();
    act(() =>
      ws.emit({ type: 'doc-updated', projectId: DOC_PID, path: REQ, version: 'v2', status: 'draft', userEdited: false, revisionId: 5, author: 'agent' }),
    );
    expect(await screen.findByRole('status')).toHaveTextContent('This doc changed — your save will conflict.');
    expect(box().value).toMatch(/typing$/);
  });

  it('takes the full viewport on a phone', async () => {
    setMedia((q) => q === PHONE_QUERY);
    const { box } = await setup();
    expect(box().closest('[data-fullscreen="true"]')).not.toBeNull();
  });

  describe('the draft survives', () => {
    it('Escape in the editor does not reach the panel sheet', async () => {
      const onWindowKey = vi.fn();
      window.addEventListener('keydown', onWindowKey);
      const { box } = await setup();
      await userEvent.type(box(), 'x{Escape}');
      expect(onWindowKey.mock.calls.some(([e]) => (e as KeyboardEvent).key === 'Escape')).toBe(false);
      window.removeEventListener('keydown', onWindowKey);
    });

    it('an unmount (sheet closed, panel switch) keeps the draft; the next editor resumes it', async () => {
      http.apiPut.mockRejectedValueOnce(conflict409());
      const first = await setup();
      await userEvent.clear(first.box());
      await userEvent.type(first.box(), 'mine');
      await userEvent.click(screen.getByRole('button', { name: 'Save' }));
      await screen.findByRole('alert');
      first.view.unmount();
      expect(first.store.getState().drafts[draftKey(DOC_PID, REQ)]?.text).toBe('mine');
      const second = await setup({}, first.store);
      expect(second.box().value).toBe('mine');
      expect(screen.getByRole('alert')).toHaveTextContent('Changed while you were editing');
    });

    it('an untouched editor leaves no draft; a save clears it', async () => {
      const { store, box } = await setup();
      expect(store.getState().drafts).toEqual({});
      await userEvent.type(box(), '!');
      expect(store.getState().drafts[draftKey(DOC_PID, REQ)]).toBeDefined();
      await userEvent.click(screen.getByRole('button', { name: 'Save' }));
      await waitFor(() => expect(store.getState().drafts).toEqual({}));
    });

    it('Cancel asks first when there are changes', async () => {
      const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);
      const { props, store, box } = await setup();
      await userEvent.type(box(), '!');
      await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(props.onCancel).not.toHaveBeenCalled();
      await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(props.onCancel).toHaveBeenCalled();
      expect(store.getState().drafts).toEqual({});
      expect(confirm).toHaveBeenCalledTimes(2);
    });

    it('Cancel without changes does not ask', async () => {
      const confirm = vi.spyOn(window, 'confirm');
      const { props } = await setup();
      await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(props.onCancel).toHaveBeenCalled();
      expect(confirm).not.toHaveBeenCalled();
    });
  });

  it('Keep editing after Show their change re-bases onto exactly the shown version', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    http.apiPut.mockRejectedValueOnce(conflict409());
    const { box } = await setup();
    await userEvent.type(box(), '!');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Show their change' }));
    await screen.findByRole('region', { name: 'Their change' });
    http.apiGet.mockResolvedValue(docRead({ version: 'v3' })); // a newer version the user has not seen
    await userEvent.click(screen.getByRole('button', { name: 'Keep editing on top of current' }));
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(http.apiPut).toHaveBeenLastCalledWith(url, expect.objectContaining({ baseVersion: 'v2' }));
    expect(http.apiGet).toHaveBeenCalledTimes(1);
  });

  it('a failing Show their change keeps the draft and the conflict', async () => {
    http.apiPut.mockRejectedValueOnce(conflict409());
    http.apiGet.mockRejectedValue(new Error('offline'));
    const { box } = await setup();
    await userEvent.clear(box());
    await userEvent.type(box(), 'mine');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Show their change' }));
    expect(await screen.findByText('offline')).toBeInTheDocument();
    expect(box().value).toBe('mine');
    expect(screen.getByRole('button', { name: 'Discard mine' })).toBeInTheDocument();
  });
});
