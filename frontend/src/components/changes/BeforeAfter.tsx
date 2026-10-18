import { useState } from 'react';
import { apiUrl } from '@/api/http';
import { agentViewUrl, DEFAULT_LAYERS } from '@/lib/viewUrls';
import { useUi } from '@/stores/ui';
import { useView } from '@/stores/view';

const seg = (on: boolean) => `rounded px-2 py-0.5 text-xs ${on ? 'bg-accent text-accent-fg' : 'text-muted hover:text-fg'}`;

/** The schematic preview of the run's snapshot (before) against the current agent view (after). */
export function BeforeAfter({ projectId, snapshotId }: { projectId: string; snapshotId: string }) {
  const [side, setSide] = useState<'before' | 'after'>('before');
  const [failed, setFailed] = useState<Record<string, boolean>>({});
  const theme = useUi((s) => s.resolvedTheme);
  const rev = useView((s) => s.revs.sch);
  // X10: <img src> takes the full /api URL.
  const before = apiUrl(`/projects/${encodeURIComponent(projectId)}/history/${encodeURIComponent(snapshotId)}/preview`);
  const after = agentViewUrl({ projectId, doc: 'sch', theme, layers: DEFAULT_LAYERS, rev });
  const src = side === 'before' ? before : after;
  return (
    <div className="flex flex-col gap-1">
      <div className="flex gap-1" role="group" aria-label="Before or after">
        <button type="button" aria-pressed={side === 'before'} className={seg(side === 'before')} onClick={() => setSide('before')}>
          Before
        </button>
        <button type="button" aria-pressed={side === 'after'} className={seg(side === 'after')} onClick={() => setSide('after')}>
          After
        </button>
      </div>
      {failed[src] ? (
        <p className="rounded bg-surface-2 p-3 text-center text-xs text-muted">
          {side === 'before' ? 'No preview was saved for this snapshot.' : 'The current view could not be rendered.'}
        </p>
      ) : (
        <img
          key={src}
          src={src}
          alt={side === 'before' ? 'Schematic before this turn' : 'Schematic now'}
          className="max-h-72 w-full rounded bg-white object-contain"
          onError={() => setFailed((f) => ({ ...f, [src]: true }))}
        />
      )}
    </div>
  );
}
