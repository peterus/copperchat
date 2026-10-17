import { GitBranch } from 'lucide-react';
import { useCallback, useState } from 'react';
import { apiGet } from '@/api/http';
import { errorText } from '@/components/history/restore';
import { useChat } from '@/hooks/useChat';
import { registerPanel } from '@/layouts/panels';
import { useReloadAfterRun } from './useReloadAfterRun';

/** GET /api/projects/{id}/git/status (Phase 7); paths are relative to the repository root. */
export interface GitStatus {
  isRepo: boolean;
  branch: string | null;
  clean: boolean;
  files: { path: string; status: string }[];
  /** All changed files; `files` is capped by the backend (`truncated`). */
  total?: number;
  truncated?: boolean;
  /** Set when git refuses the repository (e.g. dubious ownership). */
  error?: string;
}

/** The chat message the Commit button sends. It always contains the word "commit": the
 * backend's git_commit tool refuses unless the user's message asks to commit. */
export function commitRequestText(message: string): string {
  const m = message.trim();
  return m ? `Please commit the current changes. Suggested message: ${m}` : 'Please commit the current changes.';
}

export function GitPanel({ projectId }: { projectId: string }) {
  const { runActive, send } = useChat(projectId);
  const [status, setStatus] = useState<GitStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState('');

  const load = useCallback(async () => {
    try {
      setStatus(await apiGet<GitStatus>(`/projects/${projectId}/git/status`));
      setError(null);
    } catch (e) {
      setError(errorText(e));
    }
  }, [projectId]);

  useReloadAfterRun(load, runActive);

  if (error)
    return (
      <p role="alert" className="p-3 text-sm text-danger">
        {error}
      </p>
    );
  if (!status) return <p className="p-3 text-muted">Loading…</p>;
  if (!status.isRepo)
    return status.error ? (
      <p role="alert" className="p-3 text-sm text-danger">
        {status.error}
      </p>
    ) : (
      <p className="p-3 text-sm text-muted">This project is not in a git repository.</p>
    );

  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto p-3 text-fg">
      <p className="text-sm">
        Branch <span className="font-mono">{status.branch ?? '(detached)'}</span>
      </p>
      {status.clean ? (
        <p className="text-sm text-muted">No uncommitted changes.</p>
      ) : (
        <ul className="flex flex-col gap-1 font-mono text-sm">
          {status.files.map((f) => (
            <li key={f.path} className="flex gap-2">
              <span className="w-6 flex-none text-warning">{f.status}</span>
              <span className="truncate" title={f.path}>
                {f.path}
              </span>
            </li>
          ))}
          {status.truncated && (
            <li className="text-muted">… and {(status.total ?? status.files.length) - status.files.length} more</li>
          )}
        </ul>
      )}
      <label className="flex flex-col gap-1 text-sm">
        Suggested message
        <input
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          maxLength={500}
          className="rounded border border-border bg-surface px-2 py-1"
        />
      </label>
      <button
        type="button"
        disabled={status.clean || runActive}
        onClick={() => {
          if (send(commitRequestText(message))) setMessage('');
        }}
        className="self-start rounded bg-accent px-3 py-1 text-accent-fg disabled:opacity-50"
      >
        Commit via agent
      </button>
      <p className="text-xs text-muted">The agent commits only this project's files and never pushes.</p>
    </div>
  );
}

registerPanel({ id: 'git', title: 'Git', icon: GitBranch, component: GitPanel, order: 20 });
