import { History } from 'lucide-react';
import { useCallback, useState } from 'react';
import { apiGet } from '@/api/http';
import { ConfirmRestore } from '@/components/history/ConfirmRestore';
import { errorText } from '@/components/history/restore';
import { useChat } from '@/hooks/useChat';
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
}

const when = (iso: string) => new Date(iso).toLocaleString();

export function HistoryPanel({ projectId }: { projectId: string }) {
  const { runActive } = useChat(projectId);
  const [entries, setEntries] = useState<HistoryEntry[] | null>(null);
  const [confirm, setConfirm] = useState<HistoryEntry | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setEntries(await apiGet<HistoryEntry[]>(`/projects/${projectId}/history`));
      setError(null);
    } catch (e) {
      setError(errorText(e));
    }
  }, [projectId]);

  useReloadAfterRun(load, runActive);

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
      <ul className="flex flex-col gap-2">
        {entries?.map((e) => (
          <li key={e.id} className="flex items-center gap-3 rounded border border-border bg-surface p-2">
            {e.previewUrl ? (
              // previewUrl already carries the /api prefix (X10: src uses the full URL).
              <img
                src={e.previewUrl}
                alt={`Preview of ${e.label}`}
                loading="lazy"
                className="h-14 w-20 flex-none rounded bg-surface-2 object-contain"
              />
            ) : (
              <div className="h-14 w-20 flex-none rounded bg-surface-2" aria-hidden />
            )}
            <div className="min-w-0 flex-1">
              <p className="truncate font-medium" title={e.label}>
                {e.label || 'Agent turn'}
              </p>
              <p className="text-sm text-muted">{when(e.createdAt)}</p>
            </div>
            <button
              type="button"
              disabled={runActive}
              title={runActive ? 'Wait until the agent has finished' : undefined}
              onClick={() => {
                setError(null);
                setConfirm(e);
              }}
              className="rounded border border-border px-3 py-1 disabled:opacity-50"
            >
              Undo
            </button>
          </li>
        ))}
      </ul>
      {confirm && (
        <ConfirmRestore
          projectId={projectId}
          snapshotId={confirm.id}
          label={confirm.label || 'Agent turn'}
          createdAt={confirm.createdAt}
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
