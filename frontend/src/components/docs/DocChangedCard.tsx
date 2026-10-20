import { useUi } from '@/stores/ui';
import { BUTTON, docName } from './format';

/** `doc-changed` notice metadata (plan S-C8): `{action, path, revisionId, added, removed, sections}`. */
export interface DocChanged {
  path: string;
  revisionId: number | null;
  added: number;
  removed: number;
  sections: string[];
}

const count = (v: unknown) => (typeof v === 'number' && Number.isInteger(v) && v >= 0 ? v : 0);

/** The card's data from notice metadata, or null if it is not a usable `doc-changed` notice. */
export function docChangedOf(md: Record<string, unknown>): DocChanged | null {
  if (md.action !== 'doc-changed' || typeof md.path !== 'string' || !md.path) return null;
  const rid = md.revisionId;
  return {
    path: md.path,
    revisionId: typeof rid === 'number' && Number.isInteger(rid) && rid > 0 ? rid : null,
    added: count(md.added),
    removed: count(md.removed),
    sections: Array.isArray(md.sections) ? md.sections.filter((s): s is string => typeof s === 'string') : [],
  };
}

/**
 * "📄 requirements.md updated — +12 / −3 (Interfaces, Open points)" (spec §12.3). Further changes
 * in the same turn patch the row's metadata (`message-delta`), so the card re-renders in place.
 */
export function DocChangedCard({ change }: { change: DocChanged }) {
  const { path, revisionId, added, removed, sections } = change;
  const open = () =>
    useUi.getState().openPanel('docs', revisionId === null ? { path } : { path, revisionId: String(revisionId) });
  return (
    <div className="mx-auto flex max-w-full flex-wrap items-center justify-center gap-2 rounded border border-border bg-surface px-3 py-1.5 text-xs">
      <span>
        <span aria-hidden>📄 </span>
        <span className="font-mono">{docName(path)}</span> updated —{' '}
        <span className="text-success">+{added}</span> / <span className="text-danger">−{removed}</span>
        {sections.length > 0 && <span className="text-muted"> ({sections.join(', ')})</span>}
      </span>
      <button type="button" className={BUTTON} onClick={open}>
        View diff
      </button>
    </div>
  );
}
