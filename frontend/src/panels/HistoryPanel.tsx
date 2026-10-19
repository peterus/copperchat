import { History } from 'lucide-react';
import { useCallback, useMemo, useState } from 'react';
import { apiGet } from '@/api/http';
import { ConfirmRestore } from '@/components/history/ConfirmRestore';
import { errorText } from '@/components/history/restore';
import { useActiveRun, useCurrentConversationId } from '@/hooks/useChat';
import { registerPanel } from '@/layouts/panels';
import { useReloadAfterRun } from './useReloadAfterRun';

/** GET /api/projects/{id}/history entry (Phase 7). `previewUrl` is a full /api/... URL or null. */
export interface HistoryEntry {
  id: string;
  runId: string;
  createdAt: string;
  label: string;
  files: string[];
  previewUrl: string | null;
  /** `turn` (before an agent turn) or `step` (end of a plan step, E8a); absent = turn. */
  kind?: 'turn' | 'step';
  planStepId?: string | null;
  /** The conversation (session) of the snapshot's run (C1); null for older snapshots. */
  conversationId?: string | null;
  conversationTitle?: string | null;
}

const when = (iso: string) => new Date(iso).toLocaleString();
const isStep = (e: HistoryEntry) => e.kind === 'step';

interface HistoryNode {
  entry: HistoryEntry;
  /** Step checkpoints of the turn's run, in plan order (oldest first). */
  steps: HistoryEntry[];
}

/**
 * Step checkpoints nest under the turn snapshot of the same run; a step whose turn is not listed
 * (pruned) stays top-level. Server order (newest first) is kept for the top level.
 */
export function nestHistory(entries: HistoryEntry[]): HistoryNode[] {
  const turns = new Map<string, HistoryNode>();
  for (const e of entries) if (!isStep(e) && !turns.has(e.runId)) turns.set(e.runId, { entry: e, steps: [] });
  const out: HistoryNode[] = [];
  for (const e of entries) {
    if (!isStep(e)) {
      const node = turns.get(e.runId);
      out.push(node?.entry === e ? node : { entry: e, steps: [] });
      continue;
    }
    const parent = turns.get(e.runId);
    if (parent) parent.steps.push(e);
    else out.push({ entry: e, steps: [] });
  }
  for (const node of turns.values()) node.steps.sort((a, b) => a.createdAt.localeCompare(b.createdAt));
  return out;
}

function EntryRow({
  entry: e,
  blocked,
  nested,
  onRestore,
}: {
  entry: HistoryEntry;
  /** Why restoring is not possible right now, or null. */
  blocked: string | null;
  nested?: boolean;
  onRestore: (e: HistoryEntry) => void;
}) {
  const label = e.label || (isStep(e) ? 'Plan step' : 'Agent turn');
  return (
    <div className="flex items-center gap-3">
      {e.previewUrl ? (
        // previewUrl already carries the /api prefix (X10: src uses the full URL).
        <img
          src={e.previewUrl}
          alt={`Preview of ${e.label}`}
          loading="lazy"
          className={`${nested ? 'h-10 w-14' : 'h-14 w-20'} flex-none rounded bg-surface-2 object-contain`}
        />
      ) : (
        <div className={`${nested ? 'h-10 w-14' : 'h-14 w-20'} flex-none rounded bg-surface-2`} aria-hidden />
      )}
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium" title={e.label}>
          {label}
        </p>
        <p className="text-sm text-muted">{when(e.createdAt)}</p>
      </div>
      <button
        type="button"
        disabled={blocked !== null}
        title={blocked ?? undefined}
        onClick={() => onRestore(e)}
        className="rounded border border-border px-3 py-1 disabled:opacity-50"
      >
        {isStep(e) ? 'Restore' : 'Undo'}
      </button>
    </div>
  );
}

export function HistoryPanel({ projectId }: { projectId: string }) {
  // Any run of the project blocks a restore; the restore is done from the conversation shown.
  const runActive = useActiveRun(projectId) !== null;
  const conversationId = useCurrentConversationId(projectId);
  const blocked = runActive
    ? 'Wait until the agent has finished'
    : conversationId === null
      ? 'Open a session first'
      : null;
  const [entries, setEntries] = useState<HistoryEntry[] | null>(null);
  const [confirm, setConfirm] = useState<HistoryEntry | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setEntries(await apiGet<HistoryEntry[]>(`/projects/${encodeURIComponent(projectId)}/history`));
      setError(null);
    } catch (e) {
      setError(errorText(e));
    }
  }, [projectId]);

  useReloadAfterRun(load, runActive);
  const nodes = useMemo(() => (entries ? nestHistory(entries) : null), [entries]);
  const restore = useCallback((e: HistoryEntry) => {
    setError(null);
    setConfirm(e);
  }, []);

  return (
    <div className="flex h-full flex-col gap-2 overflow-y-auto p-3 text-fg">
      {error && (
        <p role="alert" className="rounded bg-danger/10 p-2 text-sm text-danger">
          {error}
        </p>
      )}
      {entries === null && !error && <p className="text-muted">Loading…</p>}
      {entries?.length === 0 && (
        <p className="text-sm text-muted">No snapshots yet. One is taken before each agent turn that changes the design.</p>
      )}
      <ul aria-label="Snapshots" className="flex flex-col gap-2">
        {nodes?.map(({ entry, steps }) => (
            <li key={entry.id} className="rounded border border-border bg-surface p-2">
              <EntryRow entry={entry} blocked={blocked} onRestore={restore} />
              {steps.length > 0 && (
                <ul
                  aria-label={`Plan steps of ${entry.label || 'Agent turn'}`}
                  className="mt-2 flex flex-col gap-2 border-l-2 border-border pl-3 sm:ml-6"
                >
                  {steps.map((st) => (
                    <li key={st.id}>
                      <EntryRow entry={st} blocked={blocked} nested onRestore={restore} />
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
      </ul>
      {confirm && conversationId !== null && (
        <ConfirmRestore
          projectId={projectId}
          conversationId={conversationId}
          snapshotId={confirm.id}
          label={confirm.label || (isStep(confirm) ? 'Plan step' : 'Agent turn')}
          createdAt={confirm.createdAt}
          checkpoint={isStep(confirm)}
          onDone={() => {
            setConfirm(null);
            void load();
          }}
          onCancel={() => setConfirm(null)}
          onError={(msg) => {
            setConfirm(null);
            setError(msg);
          }}
        />
      )}
    </div>
  );
}

registerPanel({ id: 'history', title: 'History', icon: History, component: HistoryPanel, order: 10 });
