import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  approveDoc,
  clearUserEdited,
  DocIndexDTO,
  docConflictOf,
  getDoc,
  getDocIndex,
  getRevision,
  listRevisions,
  putDoc,
  revertDoc,
  unapproveDoc,
} from './docs';
import { ApiError } from './http';

const INDEX = {
  root: 'docs/design',
  docs: [
    {
      path: 'docs/design/requirements.md',
      fixed: true,
      exists: true,
      approved: false,
      changedSinceApproval: true,
      approvedVersion: 'v0123456789abcdef',
      approvedAt: '2026-10-19T10:00:00.000Z',
      userEdited: true,
      version: 'vfedcba9876543210',
      bytes: 420,
      updatedAt: '2026-10-19T11:00:00.000Z',
      headings: ['Function', 'Open points'],
      openPoints: 2,
      lastAuthor: 'agent',
      lastConversationTitle: 'Session 2 — Schematic',
    },
  ],
  ignored: [{ path: 'docs/design/big.md', reason: 'too large' }],
};

afterEach(() => vi.unstubAllGlobals());

describe('getDocIndex', () => {
  it('fetches and parses the index', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(INDEX), { status: 200, headers: { 'content-type': 'application/json' } }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const index = await getDocIndex('a1b2c3d4e5f6');
    expect(index.docs[0]?.lastConversationTitle).toBe('Session 2 — Schematic');
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe('/api/projects/a1b2c3d4e5f6/docs');
  });

  it('rejects a wrong shape', () => {
    expect(DocIndexDTO.safeParse({ ...INDEX, ignored: undefined }).success).toBe(false);
    expect(DocIndexDTO.safeParse({ ...INDEX, docs: [{ ...INDEX.docs[0], openPoints: -1 }] }).success).toBe(false);
  });
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
const PID = 'a1b2c3d4e5f6';
const DOC = {
  path: 'docs/design/requirements.md',
  exists: true,
  version: 'v0123456789abcdef',
  meta: {
    status: 'draft',
    userEdited: false,
    approved: false,
    approvedVersion: null,
    approvedAt: null,
    changedSinceApproval: false,
  },
  body: '# Requirements\n',
};

describe('doc calls', () => {
  it('reads a doc by its path, segment-encoded', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json(DOC));
    vi.stubGlobal('fetch', fetchMock);
    const doc = await getDoc(PID, 'docs/design/notes/usb-pd.md');
    expect(doc.body).toBe('# Requirements\n');
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(`/api/projects/${PID}/docs/docs/design/notes/usb-pd.md`);
  });

  it('parses a missing fixed doc with its template', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ ...DOC, exists: false, version: null, body: '', template: '# T\n' })));
    const doc = await getDoc(PID, 'docs/design/requirements.md');
    expect(doc.template).toBe('# T\n');
    expect(doc.version).toBeNull();
  });

  it('sends the write calls with their versions', async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(json({ version: 'vnew' })));
    vi.stubGlobal('fetch', fetchMock);
    const path = 'docs/design/requirements.md';
    expect(await putDoc(PID, path, 'body', 'vbase')).toBe('vnew');
    await approveDoc(PID, path, 'v1');
    await unapproveDoc(PID, path, 'v1');
    await clearUserEdited(PID, path, 'v1');
    await revertDoc(PID, path, 7, 'vbase');
    const calls = fetchMock.mock.calls.map(([url, init]) => [
      String(url),
      (init as RequestInit).method,
      JSON.parse(String((init as RequestInit).body)) as unknown,
    ]);
    const base = `/api/projects/${PID}/docs/${path}`;
    expect(calls).toEqual([
      [base, 'PUT', { body: 'body', baseVersion: 'vbase' }],
      [`${base}/approve`, 'POST', { version: 'v1' }],
      [`${base}/unapprove`, 'POST', { version: 'v1' }],
      [`${base}/clear-user-edited`, 'POST', { version: 'v1' }],
      [`${base}/revisions/7/revert`, 'POST', { baseVersion: 'vbase' }],
    ]);
  });

  it('lists revisions and reads one', async () => {
    const rev = {
      id: 3, version: 'v3', prevVersion: 'v2', author: 'agent', conversationId: 'c'.repeat(32),
      conversationTitle: 'Session 2 — Requirements', runId: 'r1', summary: 'Interfaces', createdAt: '2026-10-19T10:00:00Z',
      added: 4, removed: 1,
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json([rev]))
      .mockResolvedValueOnce(json({ content: 'b', prevContent: null }));
    vi.stubGlobal('fetch', fetchMock);
    const revs = await listRevisions(PID, 'docs/design/parts.md');
    expect(revs[0]?.conversationTitle).toBe('Session 2 — Requirements');
    expect(await getRevision(PID, 'docs/design/parts.md', 3)).toEqual({ content: 'b', prevContent: null });
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(`/api/projects/${PID}/docs/docs/design/parts.md/revisions?limit=50`);
    expect(String(fetchMock.mock.calls[1]?.[0])).toBe(`/api/projects/${PID}/docs/docs/design/parts.md/revisions/3`);
  });

  it('extracts a doc_conflict from a 409', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        json(
          {
            error: {
              code: 'doc_conflict',
              message: 'changed',
              details: { currentVersion: 'vcur', author: 'agent', conversationTitle: 'Session 3 — Schematic', diff: '' },
            },
          },
          409,
        ),
      ),
    );
    const err = await putDoc(PID, 'docs/design/requirements.md', 'x', 'vold').catch((e: unknown) => e);
    expect(docConflictOf(err)).toEqual({ currentVersion: 'vcur', author: 'agent', conversationTitle: 'Session 3 — Schematic' });
    expect(docConflictOf(new Error('other'))).toBeNull();
  });

  it('a conflict with a deleted doc has no current version', () => {
    const err = new ApiError(409, { error: { code: 'doc_conflict', message: 'x', details: { currentVersion: null } } }, 'x');
    expect(docConflictOf(err)).toEqual({ currentVersion: null, author: null, conversationTitle: null });
  });
});
