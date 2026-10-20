import { Fragment, useMemo, useState } from 'react';
import { collapse, diffLines } from './lineDiff';

const ROW: Record<'eq' | 'add' | 'del', string> = {
  eq: 'text-muted',
  add: 'bg-success/15 text-success',
  del: 'bg-danger/15 text-danger',
};
const SIGN = { eq: ' ', add: '+', del: '-' } as const;

/** A unified (single-column) line diff: green additions, red removals, folded unchanged context. */
export function LineDiff({ before, after }: { before: string; after: string }) {
  const result = useMemo(() => diffLines(before, after), [before, after]);
  const rows = useMemo(() => (result.tooLarge ? [] : collapse(result.ops)), [result]);
  const [open, setOpen] = useState<ReadonlySet<number>>(new Set());
  if (result.tooLarge) return <p className="p-2 text-sm text-muted">Too large to diff.</p>;
  if (!result.ops.some((o) => o.t !== 'eq')) return <p className="p-2 text-sm text-muted">No changes.</p>;
  return (
    <div role="table" aria-label="Diff" className="overflow-x-auto rounded border border-border font-mono text-xs">
      {rows.map((r, i) =>
        r.kind === 'gap' && !open.has(i) ? (
          <button
            key={i}
            type="button"
            className="block w-full bg-surface-2 px-2 py-0.5 text-left text-muted"
            onClick={() => setOpen(new Set([...open, i]))}
          >
            ⋯ {r.count} unchanged {r.count === 1 ? 'line' : 'lines'}
          </button>
        ) : (
          <Fragment key={i}>
            {(r.kind === 'gap' ? r.ops : [r.op]).map((op, j) => (
            <div key={j} role="row" data-op={op.t} className={`whitespace-pre-wrap break-all px-2 ${ROW[op.t]}`}>
              <span aria-hidden className="select-none">
                {SIGN[op.t]}{' '}
              </span>
              {op.line}
            </div>
            ))}
          </Fragment>
        ),
      )}
    </div>
  );
}
