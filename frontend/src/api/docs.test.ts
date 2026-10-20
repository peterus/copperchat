import { afterEach, describe, expect, it, vi } from 'vitest';
import { DocIndexDTO, getDocIndex } from './docs';

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
