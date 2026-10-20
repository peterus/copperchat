import type { DocConflict, DocIndexEntry } from '@/api/docs';

const ROOT = 'docs/design/';

/** `docs/design/notes/usb.md` → `notes/usb.md` (also accepts a bare name). */
export const docName = (path: string): string => (path.startsWith(ROOT) ? path.slice(ROOT.length) : path);

/** The append-only log: no approval (spec §7.3). */
export const isDecisions = (path: string): boolean => docName(path) === 'decisions.md';

export type DocBadge = 'missing' | 'draft' | 'approved' | 'changed since approval';

export function docBadge(d: Pick<DocIndexEntry, 'exists' | 'approved' | 'changedSinceApproval'>): DocBadge {
  if (!d.exists) return 'missing';
  if (d.approved) return 'approved';
  return d.changedSinceApproval ? 'changed since approval' : 'draft';
}

export const BADGE_CLASS: Record<DocBadge, string> = {
  missing: 'border-border text-muted',
  draft: 'border-border text-fg',
  approved: 'border-success text-success',
  'changed since approval': 'border-warning text-warning',
};

const AUTHOR: Record<string, string> = { agent: 'agent', user: 'you', external: 'external edit' };

/** "agent · Session 2 — Schematic", "you", … */
export function changedBy(author: string | null, session: string | null): string | null {
  const who = author === null ? null : (AUTHOR[author] ?? author);
  return [who, session].filter(Boolean).join(' · ') || null;
}

/** "Session 3 — Schematic", or who else changed the doc (409 `doc_conflict`). */
export function conflictBy(c: DocConflict): string {
  if (c.conversationTitle) return c.conversationTitle;
  if (c.author === 'agent') return 'the agent';
  if (c.author === 'user') return 'you, in another window';
  if (c.author === 'external') return 'an edit outside CopperChat';
  return 'someone else';
}

/** A doc link `../parts/<MPN>/notes.md` (spec §12.2) → the MPN, else null. */
export function partOfLink(href: string): string | null {
  const m = /^\.\.\/parts\/([^/?#]+)\/notes\.md$/.exec(href);
  if (!m) return null;
  try {
    const mpn = decodeURIComponent(m[1]!);
    const bad = [...mpn].some((c) => c === '/' || c === '\\' || c.charCodeAt(0) < 0x20 || c === '\u007f');
    return mpn && !bad && mpn !== '.' && mpn !== '..' ? mpn : null;
  } catch {
    return null;
  }
}

export const BUTTON = 'rounded border border-border px-2 py-1 text-xs hover:bg-surface-2 disabled:opacity-50';
export const PRIMARY = 'rounded bg-accent px-2 py-1 text-xs font-medium text-accent-fg disabled:opacity-50';

/**
 * A stored revision text without its front matter, as the backend splits it
 * (`design_docs/frontmatter.split`): a BOM is dropped, the block opens and closes with a line
 * that is `---` after trimming; an unterminated block means the whole text is body.
 */
export function docBody(text: string): string {
  const t = text.startsWith('\ufeff') ? text.slice(1) : text;
  const lines = t.match(/[^\n]*\n|[^\n]+$/g) ?? [];
  if (lines.length === 0 || lines[0]!.trim() !== '---') return t;
  for (let i = 1; i < lines.length; i++) {
    if (lines[i]!.trim() === '---') return lines.slice(i + 1).join('');
  }
  return t;
}
