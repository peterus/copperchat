import { useMemo } from 'react';
import type { ChangeEntry } from '@/api/changes';

const OP_LABEL: Record<string, string> = { add: 'Added', modify: 'Modified', delete: 'Deleted' };
const KIND_CLASS: Record<string, string> = { add: 'text-success', modify: 'text-warning', delete: 'text-danger' };

/** What the entry is about: reference, net or a short UUID. */
export function entryName(e: ChangeEntry): string {
  return e.ref ?? e.net ?? e.uuid.slice(0, 8);
}

/**
 * The entries of a run grouped by plan step (`stepIds` order; entries outside a step first).
 * Side effects (KiCad cleanup the agent did not ask for) are marked.
 */
export function ChangeList({
  entries,
  stepIds,
  onPick,
}: {
  entries: ChangeEntry[];
  stepIds: string[];
  onPick: (e: ChangeEntry) => void;
}) {
  const groups = useMemo(() => {
    const out = new Map<string | null, ChangeEntry[]>();
    for (const e of [...entries].sort((a, b) => a.callSeq - b.callSeq || a.id - b.id)) {
      const list = out.get(e.planStepId);
      if (list) list.push(e);
      else out.set(e.planStepId, [e]);
    }
    return out;
  }, [entries]);
  const order = [null, ...stepIds, ...[...groups.keys()].filter((k) => k !== null && !stepIds.includes(k))];
  const title = (step: string | null) =>
    step === null ? 'Outside a plan step' : `Plan step ${stepIds.includes(step) ? stepIds.indexOf(step) + 1 : '?'}`;
  return (
    <div className="flex flex-col gap-2">
      {order
        .filter((step, i) => groups.has(step) && order.indexOf(step) === i)
        .map((step) => (
          <div key={step ?? ''} role="group" aria-label={title(step)}>
            <h4 className="text-xs font-medium text-muted">{title(step)}</h4>
            <ul className="flex flex-col">
              {groups.get(step)!.map((e) => (
                <li key={e.id}>
                  <button
                    type="button"
                    onClick={() => onPick(e)}
                    aria-label={`${OP_LABEL[e.op] ?? e.op} ${e.kind} ${entryName(e)}${e.sideEffect ? ', side effect' : ''} (${e.tool})`}
                    className="flex w-full items-baseline gap-2 rounded px-1 py-1 text-left text-sm hover:bg-surface-2"
                  >
                    <span className={`shrink-0 font-medium ${KIND_CLASS[e.op] ?? ''}`}>{OP_LABEL[e.op] ?? e.op}</span>
                    <span className="min-w-0 flex-1 truncate">
                      {e.kind} {entryName(e)}
                    </span>
                    {e.sideEffect && <span className="shrink-0 rounded bg-warning/15 px-1 text-xs">side effect</span>}
                    <span className="shrink-0 text-xs text-muted">{e.tool}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ))}
    </div>
  );
}
