import type { DocIndexEntry, DocRead, RevisionDTO } from '@/api/docs';
import { ApiError } from '@/api/http';

export const DOC_PID = 'a1b2c3d4e5f6';
export const REQ = 'docs/design/requirements.md';

export const docEntry = (path: string, over: Partial<DocIndexEntry> = {}): DocIndexEntry => ({
  path, fixed: true, exists: true, approved: false, changedSinceApproval: false, approvedVersion: null, approvedAt: null,
  userEdited: false, version: 'v1', bytes: 10, updatedAt: null, headings: [], openPoints: 0, lastAuthor: null,
  lastConversationTitle: null, ...over,
});

export const docRead = (over: Omit<Partial<DocRead>, 'meta'> & { meta?: Partial<DocRead['meta']> } = {}): DocRead => {
  const { meta, ...rest } = over;
  return {
    path: REQ, exists: true, version: 'v1', body: '# Requirements\n\nUSB-C power.\n',
    ...rest,
    meta: { status: 'draft', userEdited: false, approved: false, approvedVersion: null, approvedAt: null,
      changedSinceApproval: false, ...meta },
  };
};

export const revision = (id: number, over: Partial<RevisionDTO> = {}): RevisionDTO => ({
  id, version: `v${id}`, prevVersion: id > 1 ? `v${id - 1}` : null, author: 'agent', conversationId: null,
  conversationTitle: 'Session 2 — Requirements', runId: null, summary: `change ${id}`, createdAt: '2026-10-19T10:00:00Z',
  added: id, removed: 1, ...over,
});

/** The 409 the backend sends for a stale base version. */
export const conflict409 = (details: Record<string, unknown> = {}) =>
  new ApiError(
    409,
    { error: { code: 'doc_conflict', message: 'changed', details: { currentVersion: 'v2', author: 'agent', conversationTitle: 'Session 3 — Schematic', ...details } } },
    'changed',
  );
