import { GitCompareArrows } from 'lucide-react';
import { memo, useEffect, useMemo, useRef, useState } from 'react';
import { type ChangeEntry, type ChangeRun, type ChangesDiff, fetchChangesDiff } from '@/api/changes';
import { BeforeAfter } from '@/components/changes/BeforeAfter';
import { ChangeList } from '@/components/changes/ChangeList';
import { ErcBadge } from '@/components/changes/ErcBadge';
import { diffHighlight, entriesHighlight } from '@/components/changes/highlight';
import { errorText } from '@/components/history/restore';
import { useChat, useCurrentConversationId } from '@/hooks/useChat';
import { registerPanel } from '@/layouts/panels';
import { entriesKey, useChanges } from '@/stores/changes';
import { useView } from '@/stores/view';

const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : 'unknown time');
const time = (iso: string | null) => (iso ? Date.parse(iso) : Number.NaN);
const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? '' : 's'}`;
const KIND_LABEL = { added: 'Added', modified: 'Modified', removed: 'Removed' } as const;

/** A user or automatic history restore (notice `metadata.action === 'restored'`). */
interface Restore {
  id: number;
  at: string;
  snapshotId: string;
}

/**
 * Restore boundaries (E8a residual): a restore after a run puts the design back to some
 * snapshot. Restoring to run R's turn snapshot undoes R and every later run before the restore;
 * the diff of a run with a later restore compares against the restored state, so its unlogged
 * entries may come from the restore rather than from KiCad's cleanup.
 */
function restoreInfo(runs: ChangeRun[], restores: Restore[]) {
  const undone = new Set<string>();
  const restoredAfter = new Set<string>();
  for (const r of restores) {
    const target = runs.find((run) => run.snapshotId === r.snapshotId);
    for (const run of runs) {
      if (!(time(run.createdAt) < time(r.at))) continue;
      restoredAfter.add(run.runId);
      if (target && time(run.createdAt) >= time(target.createdAt)) undone.add(run.runId);
    }
  }
  return { undone, restoredAfter };
}

function DiffList({ diff, restoredAfter, onPick }: { diff: ChangesDiff; restoredAfter: boolean; onPick: (sheet: string, i: number) => void }) {
  const sheets = Object.entries(diff.sheets).filter(([, list]) => list.length);
  return (
    <div className="flex flex-col gap-1 text-sm">
      <p className="text-xs text-muted">
        {plural(diff.count, 'difference')} to the snapshot before this turn
        {sheets.length > 1 ? ` on ${sheets.length} sheets` : ''}.
      </p>
      {restoredAfter && (
        <p role="note" className="rounded bg-warning/10 p-2 text-xs">
          The design was restored after this turn: the diff compares against the restored design, and items without a
          logged change may come from that restore.
        </p>
      )}
      {sheets.map(([sheet, list]) => (
        <ul key={sheet} aria-label={`Differences on sheet ${sheet}`}>
          {list.map((d, i) => (
            <li key={d.id}>
              <button
                type="button"
                onClick={() => onPick(sheet, i)}
                aria-label={`${KIND_LABEL[d.kind]} ${d.type}${d.ref ? ` ${d.ref}` : ''}${d.sideEffect ? `, ${restoredAfter ? 'restore or side effect' : 'side effect'}` : ''}`}
                className="flex w-full flex-wrap items-baseline gap-x-2 rounded px-1 py-1 text-left hover:bg-surface-2"
              >
                <span className={d.kind === 'added' ? 'text-success' : d.kind === 'removed' ? 'text-danger' : 'text-warning'}>
                  {KIND_LABEL[d.kind]}
                </span>
                <span>
                  {d.type} {d.ref ?? ''}
                </span>
                {d.sideEffect && (
                  <span className="rounded bg-warning/15 px-1 text-xs">{restoredAfter ? 'restore or side effect' : 'side effect'}</span>
                )}
                {d.kind === 'modified' &&
                  d.properties.map((p) => (
                    <span key={p.name} className="w-full pl-4 text-xs text-muted">
                      {p.name}: {String(p.before ?? '')} → {String(p.after ?? '')}
                    </span>
                  ))}
              </button>
            </li>
          ))}
        </ul>
      ))}
    </div>
  );
}

const RunCard = memo(function RunCard({
  projectId,
  run,
  undone,
  restoredAfter,
}: {
  projectId: string;
  run: ChangeRun;
  undone: boolean;
  restoredAfter: boolean;
}) {
  const open = useChanges((s) => s.expanded[run.runId] ?? false);
  const setExpanded = useChanges((s) => s.setExpanded);
  const diffLayer = useChanges((s) => s.diffLayer);
  const setDiffLayer = useChanges((s) => s.setDiffLayer);
  const highlightSeq = useView((s) => s.highlight?.seq ?? null);
  // the diff layer is on while the highlight it set is still shown
  const diffOn = diffLayer?.runId === run.runId && diffLayer.seq === highlightSeq;
  const [diff, setDiff] = useState<ChangesDiff | null>(null);
  /** Open + `revs.sch` the diff was computed at: reused until the schematic changes. */
  const [diffRev, setDiffRev] = useState<string | null>(null);
  const schRev = useView((s) => `${s.openSeq}:${s.revs.sch}`);
  const [diffError, setDiffError] = useState<string | null>(null);
  const [compare, setCompare] = useState(false);
  /** Diff requested, not loaded yet. */
  const [pending, setPendingState] = useState(false);
  const pendingRef = useRef(false);
  const setPending = (p: boolean) => {
    pendingRef.current = p;
    setPendingState(p);
  };
  const entries = useChanges((s) => s.entries[entriesKey(run.runId)]);
  const loadEntries = useChanges((s) => s.loadEntries);
  const setHighlight = useView((s) => s.setHighlight);
  const clearHighlight = useView((s) => s.clearHighlight);

  useEffect(() => {
    if (open && !entries) void loadEntries(run.runId);
  }, [open, entries, loadEntries, run.runId]);

  // remounted with the layer on (phone tab switch): fetch the list again, keep the highlight
  const snapshotId = run.snapshotId;
  useEffect(() => {
    if (!diffOn || diff || !snapshotId) return;
    const rev = schRev;
    fetchChangesDiff(projectId, snapshotId).then(
      (d) => {
        setDiff(d);
        setDiffRev(rev);
      },
      (e: unknown) => setDiffError(`Could not compute the diff: ${errorText(e)}`),
    );
    // only on (re)mount with the layer on
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // the schematic changed while the diff layer is shown: its boxes are outdated, so it goes off
  const [diffOutdated, setDiffOutdated] = useState(false);
  useEffect(() => {
    if (!diffOn || !diff || diffRev === schRev) return;
    setDiffLayer(null);
    clearHighlight();
    setDiffOutdated(true);
  }, [diffOn, diff, diffRev, schRev, setDiffLayer, clearHighlight]);

  const pick = (e: ChangeEntry) => setHighlight(entriesHighlight([e], diff));
  const toggleDiff = () => {
    if (diffOn || pending) {
      setPending(false);
      setDiffLayer(null);
      if (diffOn) clearHighlight();
      return;
    }
    if (!snapshotId) return;
    setDiffError(null);
    setDiffOutdated(false);
    if (diff && diffRev === schRev) {
      setDiffLayer({ runId: run.runId, seq: setHighlight(diffHighlight(diff)) });
      return;
    }
    setPending(true);
    const rev = schRev;
    fetchChangesDiff(projectId, snapshotId).then(
      (d) => {
        setDiff(d);
        setDiffRev(rev);
        if (!pendingRef.current) return; // switched off meanwhile
        setPending(false);
        setDiffLayer({ runId: run.runId, seq: setHighlight(diffHighlight(d)) });
      },
      (e: unknown) => {
        setPending(false);
        setDiffError(`Could not compute the diff: ${errorText(e)}`);
      },
    );
  };
  const btn = (on: boolean) =>
    `rounded border px-2 py-0.5 text-xs disabled:opacity-50 ${on ? 'border-accent bg-accent text-accent-fg' : 'border-border'}`;

  return (
    <li aria-label={`Turn ${run.runId} at ${when(run.createdAt)}`} className="rounded border border-border bg-surface p-2">
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium">{when(run.createdAt)}</p>
          <p className="text-xs text-muted">
            {plural(run.count, 'change')}
            {run.sideEffects > 0 && ` · ${plural(run.sideEffects, 'side effect')}`}
            {run.stepIds.length > 0 && ` · ${plural(run.stepIds.length, 'plan step')}`}
            {undone && <span className="ml-1 rounded bg-surface-2 px-1">Undone by a restore</span>}
          </p>
        </div>
        <ErcBadge projectId={projectId} erc={run.erc} />
        <button type="button" aria-expanded={open} onClick={() => setExpanded(run.runId, !open)} className="rounded border border-border px-2 py-0.5 text-xs">
          {open ? 'Hide changes' : 'Show changes'}
        </button>
      </div>
      {open && (
        <div className="mt-2 flex flex-col gap-2">
          <div className="flex flex-wrap gap-1">
            <button
              type="button"
              className={btn(false)}
              disabled={!entries?.length}
              onClick={() => entries && setHighlight(entriesHighlight(entries, diff))}
            >
              Show all changes
            </button>
            <button
              type="button"
              aria-pressed={diffOn || pending}
              className={btn(diffOn || pending)}
              disabled={!run.snapshotId}
              title={run.snapshotId ? undefined : 'No snapshot of this turn is kept'}
              onClick={toggleDiff}
            >
              Diff
            </button>
            <button
              type="button"
              aria-pressed={compare}
              className={btn(compare)}
              disabled={!run.snapshotId}
              onClick={() => setCompare((c) => !c)}
            >
              Before / after
            </button>
          </div>
          {compare && run.snapshotId && <BeforeAfter projectId={projectId} snapshotId={run.snapshotId} />}
          {entries ? <ChangeList entries={entries} stepIds={run.stepIds} onPick={pick} /> : <p className="text-xs text-muted">Loading…</p>}
          {diffOutdated && (
            <p role="status" className="text-xs text-muted">
              The schematic changed: turn Diff on again for the current state.
            </p>
          )}
          {diffError && (
            <p role="alert" className="text-xs text-danger">
              {diffError}
            </p>
          )}
          {pending && <p className="text-xs text-muted">Computing the diff…</p>}
          {(diffOn || pending) && diff && (
            <DiffList
              diff={diff}
              restoredAfter={restoredAfter}
              onPick={(sheet, i) => {
                const d = diff.sheets[sheet]![i]!;
                // still the diff layer's list: picking a row keeps it open
                const seq = setHighlight({
                  refs: d.boxMm || !d.ref ? [] : [{ ref: d.ref, kind: d.kind }],
                  boxes: d.boxMm ? [{ ...d.boxMm, kind: d.kind }] : [],
                  sheetPath: sheet,
                });
                setDiffLayer({ runId: run.runId, seq });
              }}
            />
          )}
        </div>
      )}
    </li>
  );
});

export function ChangesPanel({ projectId }: { projectId: string }) {
  const runs = useChanges((s) => (s.projectId === projectId ? s.runs : null));
  const error = useChanges((s) => s.error);
  const load = useChanges((s) => s.load);
  const attach = useChanges((s) => s.attach);
  useEffect(() => attach(), [attach]);
  // Restore notices of the conversation shown (C1 interim: one default session).
  const { messages } = useChat(projectId, useCurrentConversationId(projectId));

  useEffect(() => {
    void load(projectId);
  }, [load, projectId]);

  // messages change with every streamed token: derive a stable key of the restore notices first
  const restoresKey = JSON.stringify(
    messages
      .filter((m) => m.kind === 'notice' && m.metadata.action === 'restored' && typeof m.metadata.snapshotId === 'string')
      .map((m): Restore => ({ id: m.id, at: m.createdAt, snapshotId: m.metadata.snapshotId as string })),
  );
  const { undone, restoredAfter, timeline } = useMemo(() => {
    const restores = JSON.parse(restoresKey) as Restore[];
    // newest first; a restore sits between the runs before and after it
    const timeline = [
      ...(runs ?? []).map((run) => ({ at: time(run.createdAt), run })),
      ...restores.map((restore) => ({ at: time(restore.at), restore })),
    ].sort((a, b) => b.at - a.at);
    return { timeline, ...restoreInfo(runs ?? [], restores) };
  }, [runs, restoresKey]);
  return (
    <div className="flex h-full flex-col gap-2 overflow-y-auto p-3 text-fg">
      {error && (
        <p role="alert" className="rounded bg-danger/10 p-2 text-sm text-danger">
          {error}
        </p>
      )}
      {runs === null && !error && <p className="text-muted">Loading…</p>}
      {runs?.length === 0 && <p className="text-sm text-muted">No changes yet. Edits the agent makes to the schematic are listed here per turn.</p>}
      <ul className="flex flex-col gap-2">
        {timeline.map((t) =>
          'run' in t && t.run ? (
            <RunCard
              key={t.run.runId}
              projectId={projectId}
              run={t.run}
              undone={undone.has(t.run.runId)}
              restoredAfter={restoredAfter.has(t.run.runId)}
            />
          ) : 'restore' in t && t.restore && runs?.length ? (
            <li
              key={`restore-${t.restore.id}`}
              aria-label={`Restored at ${when(t.restore.at)}`}
              className="border-t border-dashed border-border pt-1 text-center text-xs text-muted"
            >
              Design restored to an earlier snapshot · {when(t.restore.at)}
            </li>
          ) : null,
        )}
      </ul>
    </div>
  );
}

registerPanel({ id: 'changes', title: 'Changes', icon: GitCompareArrows, component: ChangesPanel, order: 15 });
