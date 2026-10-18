import { useState } from 'react';
import { type ChangeRun, type ErcResult, fetchErcResult } from '@/api/changes';
import { errorText } from '@/components/history/restore';

const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? '' : 's'}`;

/**
 * New ERC errors (red) / warnings (amber) of a run against its baseline; the details list the
 * violations, errors the agent justified with its reason.
 */
export function ErcBadge({ projectId, erc }: { projectId: string; erc: ChangeRun['erc'] }) {
  const [open, setOpen] = useState(false);
  const [result, setResult] = useState<ErcResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (!erc) return null;
  const toggle = () => {
    setOpen((o) => !o);
    if (result || open) return;
    fetchErcResult(projectId, erc.resultId).then(
      (r) => setResult(r),
      (e: unknown) => setError(`Could not load the ERC result: ${errorText(e)}`),
    );
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
          {error && <p role="alert" className="text-danger">{error}</p>}
          {!result && !error && <p className="text-muted">Loading…</p>}
          {result && (
            <ul className="flex flex-col gap-1">
              {[...result.newErrors.map((v) => ({ v, sev: 'error' as const })), ...result.newWarnings.map((v) => ({ v, sev: 'warning' as const }))].map(
                ({ v, sev }, i) => {
                  const reason = v.id ? result.justifications[v.id] : undefined;
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
