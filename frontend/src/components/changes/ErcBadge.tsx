import { useEffect, useState } from 'react';
import { type ChangeRun, type ErcResult, fetchErcResult } from '@/api/changes';
import { errorText } from '@/components/history/restore';

const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? '' : 's'}`;

/**
 * New ERC errors (red) / warnings (amber) of a run against its baseline; the details list the
 * violations, errors the agent justified with its reason.
 */
export function ErcBadge({ projectId, erc }: { projectId: string; erc: ChangeRun['erc'] }) {
  const [open, setOpen] = useState(false);
  const [loaded, setLoaded] = useState<ErcResult | null>(null);
  const [error, setError] = useState<{ id: string; text: string } | null>(null);
  const resultId = erc?.resultId ?? null;
  // A later plan step gives the run a newer ERC result: the details follow it.
  const result = loaded && loaded.id === resultId ? loaded : null;
  const failure = error && error.id === resultId ? error.text : null;
  useEffect(() => {
    if (!open || !resultId || result) return;
    let gone = false;
    fetchErcResult(projectId, resultId).then(
      (r) => !gone && setLoaded(r),
      (e: unknown) => !gone && setError({ id: resultId, text: `Could not load the ERC result: ${errorText(e)}` }),
    );
    return () => {
      gone = true;
    };
  }, [open, resultId, result, projectId]);
  if (!erc) return null;
  const toggle = () => {
    if (!open) setError(null);
    setOpen((o) => !o);
  };
  const clean = erc.errors === 0 && erc.warnings === 0;
  return (
    <div className="text-xs">
      <button
        type="button"
        aria-expanded={open}
        disabled={clean}
        onClick={toggle}
        aria-label={clean ? 'ERC: no new problems' : `${plural(erc.errors, 'new ERC error')}, ${plural(erc.warnings, 'warning')}`}
        className={`rounded px-1.5 py-0.5 font-medium ${erc.errors ? 'bg-danger/15 text-danger' : clean ? 'text-muted' : 'bg-warning/15'}`}
      >
        {clean ? (
          'ERC clean'
        ) : (
          <>
            {erc.errors > 0 && <span>{plural(erc.errors, 'ERC error')}</span>}
            {erc.errors > 0 && erc.warnings > 0 && ' · '}
            {erc.warnings > 0 && <span className="text-warning">{plural(erc.warnings, 'warning')}</span>}
          </>
        )}
      </button>
      {open && (
        <div className="mt-1 rounded border border-border p-2">
          {failure && <p role="alert" className="text-danger">{failure}</p>}
          {!result && !failure && <p className="text-muted">Loading…</p>}
          {result && (
            <ul className="flex flex-col gap-1">
              {[...result.newErrors.map((v) => ({ v, sev: 'error' as const })), ...result.newWarnings.map((v) => ({ v, sev: 'warning' as const }))].map(
                ({ v, sev }, i) => {
                  const reason = v.id && Object.hasOwn(result.justifications, v.id) ? result.justifications[v.id] : undefined;
                  return (
                    <li key={v.id ?? i}>
                      <span className={sev === 'error' ? 'text-danger' : 'text-warning'}>{sev === 'error' ? 'Error' : 'Warning'}: </span>
                      <span>{v.description || v.type}</span>
                      {reason && <span className="block text-muted">Justified: {reason}</span>}
                    </li>
                  );
                },
              )}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
