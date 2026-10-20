import { describe, expect, it, vi } from 'vitest';
import type { DocIndexDTO, DocIndexEntry, DocRead } from '@/api/docs';
import { FakeWsClient } from '@/test/fakeWsClient';
import { createDocsStore, type DocsDeps, draftKey } from './docs';

const PID = 'a1b2c3d4e5f6';
const entry = (path: string, over: Partial<DocIndexEntry> = {}): DocIndexEntry => ({
  path, fixed: false, exists: true, approved: false, changedSinceApproval: false, approvedVersion: null, approvedAt: null,
  userEdited: false, version: 'v1', bytes: 10, updatedAt: null, headings: [], openPoints: 0, lastAuthor: null,
  lastConversationTitle: null, ...over,
});
const INDEX: DocIndexDTO = {
  root: 'docs/design',
  docs: [
    entry('docs/design/notes/usb.md'),
    entry('docs/design/requirements.md', { fixed: true }),
    entry('docs/design/board.md', { fixed: true, exists: false, version: null }),
    entry('docs/design/a.md'),
  ],
  ignored: [{ path: 'docs/design/link.md', reason: 'symlink' }],
};
const doc = (version: string, body = 'body'): DocRead => ({
  path: 'docs/design/requirements.md', exists: true, version, body,
  meta: { status: 'draft', userEdited: false, approved: false, approvedVersion: null, approvedAt: null, changedSinceApproval: false },
});
const frame = (path: string, version: string) => ({
  type: 'doc-updated', projectId: PID, path, version, status: 'draft', userEdited: false, revisionId: 4, author: 'agent',
});

function setup(over: Partial<DocsDeps> = {}) {
  const ws = new FakeWsClient();
  const deps: DocsDeps = {
    getIndex: vi.fn().mockResolvedValue(INDEX),
    getDoc: vi.fn().mockResolvedValue(doc('v1')),
    ...over,
  };
  return { ws, deps, store: createDocsStore(ws, deps) };
}

describe('docs store', () => {
  it('loads the index: fixed docs first, then extra docs, then the ignored entries', async () => {
    const { store } = setup();
    await store.getState().load(PID);
    const s = store.getState();
    expect(s.index?.docs.map((d) => d.path)).toEqual([
      'docs/design/requirements.md',
      'docs/design/board.md',
      'docs/design/notes/usb.md',
      'docs/design/a.md',
    ]);
    expect(s.index?.ignored).toEqual([{ path: 'docs/design/link.md', reason: 'symlink' }]);
  });

  it('a doc-updated frame refreshes the index and the open doc', async () => {
    const { store, ws, deps } = setup();
    await store.getState().load(PID);
    await store.getState().open('docs/design/requirements.md');
    expect(store.getState().doc?.version).toBe('v1');
    vi.mocked(deps.getDoc).mockResolvedValue(doc('v2', 'new'));
    ws.emit(frame('docs/design/requirements.md', 'v2'));
    await vi.waitFor(() => expect(store.getState().doc?.version).toBe('v2'));
    expect(deps.getIndex).toHaveBeenCalledTimes(2);
    expect(store.getState().live['docs/design/requirements.md']?.version).toBe('v2');
  });

  it('a frame of another doc or project does not reload the open doc', async () => {
    const { store, ws, deps } = setup();
    await store.getState().load(PID);
    await store.getState().open('docs/design/requirements.md');
    ws.emit(frame('docs/design/parts.md', 'v9'));
    ws.emit({ ...frame('docs/design/requirements.md', 'v9'), projectId: 'ffffffffffff' });
    await vi.waitFor(() => expect(deps.getIndex).toHaveBeenCalledTimes(2));
    expect(deps.getDoc).toHaveBeenCalledTimes(1);
    expect(store.getState().live['docs/design/requirements.md']).toBeUndefined();
  });

  it('coalesces a burst of frames into one more index fetch', async () => {
    let release: () => void = () => {};
    const getIndex = vi.fn().mockResolvedValueOnce(INDEX);
    const { store, ws } = setup({ getIndex });
    await store.getState().load(PID);
    getIndex.mockImplementation(() => new Promise<DocIndexDTO>((r) => (release = () => r(INDEX))));
    for (let i = 2; i < 7; i++) ws.emit(frame('docs/design/parts.md', `v${i}`));
    release();
    await vi.waitFor(() => expect(getIndex).toHaveBeenCalledTimes(3));
    release();
    await new Promise((r) => setTimeout(r, 10));
    expect(getIndex).toHaveBeenCalledTimes(3);
  });

  it('drops a response of the previous project', async () => {
    let late: (v: DocIndexDTO) => void = () => {};
    const getIndex = vi
      .fn()
      .mockImplementationOnce(() => new Promise<DocIndexDTO>((r) => (late = r)))
      .mockResolvedValueOnce({ ...INDEX, docs: [] });
    const { store } = setup({ getIndex });
    const first = store.getState().load(PID);
    await store.getState().load('ffffffffffff');
    late(INDEX);
    await first;
    expect(store.getState().projectId).toBe('ffffffffffff');
    expect(store.getState().index?.docs).toEqual([]);
  });

  it('a stale open() response does not replace a newer one', async () => {
    let late: (v: DocRead) => void = () => {};
    const getDoc = vi
      .fn()
      .mockImplementationOnce(() => new Promise<DocRead>((r) => (late = r)))
      .mockResolvedValueOnce({ ...doc('v5'), path: 'docs/design/parts.md' });
    const { store } = setup({ getDoc });
    await store.getState().load(PID);
    const first = store.getState().open('docs/design/requirements.md');
    await store.getState().open('docs/design/parts.md');
    late(doc('v1'));
    await first;
    expect(store.getState().doc?.path).toBe('docs/design/parts.md');
  });

  it('shows a load failure', async () => {
    const { store } = setup({ getIndex: vi.fn().mockRejectedValue(new Error('boom')) });
    await store.getState().load(PID);
    expect(store.getState().indexError).toMatch(/boom/);
  });

  it('refreshes the index and the open doc after a reconnect', async () => {
    const { store, ws, deps } = setup();
    await store.getState().load(PID);
    await store.getState().open('docs/design/requirements.md');
    ws.connect();
    await vi.waitFor(() => expect(deps.getDoc).toHaveBeenCalledTimes(2));
    expect(deps.getIndex).toHaveBeenCalledTimes(2);
  });

  it('an older doc response never replaces a newer one (refresh vs refresh)', async () => {
    const resolvers: Array<(d: DocRead) => void> = [];
    const getDoc = vi.fn().mockResolvedValueOnce(doc('v1'));
    const { store, ws } = setup({ getDoc });
    await store.getState().load(PID);
    await store.getState().open('docs/design/requirements.md');
    getDoc.mockImplementation(() => new Promise<DocRead>((r) => resolvers.push(r)));
    ws.emit(frame('docs/design/requirements.md', 'v2'));
    await vi.waitFor(() => expect(resolvers).toHaveLength(1));
    // a reconnect starts another (non-coalesced) open of the same doc
    const reopen = store.getState().open('docs/design/requirements.md');
    await vi.waitFor(() => expect(resolvers).toHaveLength(2));
    resolvers[1]!(doc('v3'));
    await reopen;
    resolvers[0]!(doc('v2'));
    await new Promise((r) => setTimeout(r, 0));
    expect(store.getState().doc?.version).toBe('v3');
  });

  it('after detach() frames and reconnects no longer refetch', async () => {
    const { store, ws, deps } = setup();
    await store.getState().load(PID);
    await store.getState().open('docs/design/requirements.md');
    store.getState().detach();
    expect(store.getState().openPath).toBeNull();
    ws.emit(frame('docs/design/requirements.md', 'v2'));
    ws.connect();
    await new Promise((r) => setTimeout(r, 0));
    expect(deps.getIndex).toHaveBeenCalledTimes(1);
    expect(deps.getDoc).toHaveBeenCalledTimes(1);
    expect(store.getState().live['docs/design/requirements.md']?.version).toBe('v2');
  });

  it('keeps drafts per project across project switches and asks before unload', async () => {
    const { store } = setup();
    await store.getState().load(PID);
    const draft = { text: 'mine', base: { version: 'v1', body: 'b', approved: false }, conflict: null };
    store.getState().setDraft(PID, 'docs/design/requirements.md', draft);
    await store.getState().load('ffffffffffff');
    expect(store.getState().drafts[draftKey(PID, 'docs/design/requirements.md')]).toEqual(draft);
    const ev = new Event('beforeunload', { cancelable: true }) as BeforeUnloadEvent;
    window.dispatchEvent(ev);
    expect(ev.defaultPrevented).toBe(true);
    store.getState().setDraft(PID, 'docs/design/requirements.md', null);
    const ev2 = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(ev2);
    expect(ev2.defaultPrevented).toBe(false);
    store.getState().dispose();
  });
});
