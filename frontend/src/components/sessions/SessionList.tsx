import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { CloseIcon } from '@/components/icons';
import { useMediaQuery } from '@/hooks/useMediaQuery';
import { PHONE_QUERY } from '@/layouts/ProjectLayout';
import type { ConversationDTO } from '@/protocol/frames';
import { useConversations, useConversationsApi } from '@/stores/conversations';
import { TitleEdit } from './TitleEdit';
import { TypeIcon } from './TypeIcon';

const EMPTY: ConversationDTO[] = [];
const NONE: string[] = [];

export const sessionPath = (projectId: string, conversationId: string) => `/p/${projectId}/s/${conversationId}`;

const when = (c: ConversationDTO) => new Date(c.lastMessageAt ?? c.createdAt).toLocaleString();
const cost = (usd: number) => `$${usd.toFixed(2)}`;
const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

function Row({
  projectId,
  c,
  current,
  running,
  unread,
  onPick,
  onError,
}: {
  projectId: string;
  c: ConversationDTO;
  current: boolean;
  running: boolean;
  unread: boolean;
  onPick: () => void;
  onError: (message: string | null) => void;
}) {
  const store = useConversationsApi();
  const [renaming, setRenaming] = useState(false);
  const act = (fn: () => Promise<void>) => {
    onError(null);
    fn().catch((e: unknown) => onError(errorText(e)));
  };
  const btn = 'rounded px-1.5 py-0.5 text-xs text-muted hover:bg-surface-2 hover:text-fg';
  return (
    <li data-closed={c.status === 'closed' ? 'true' : undefined} className={`flex flex-col gap-1 rounded px-2 py-1.5 ${c.status === 'closed' ? 'opacity-60' : ''} ${current ? 'bg-surface-2' : ''}`}>
      {renaming ? (
        <TitleEdit
          initial={c.title}
          onCancel={() => setRenaming(false)}
          onSave={async (title) => {
            await store.getState().rename(projectId, c.id, title);
            setRenaming(false);
          }}
        />
      ) : (
        <Link
          to={sessionPath(projectId, c.id)}
          onClick={onPick}
          aria-current={current ? 'page' : undefined}
          className="flex min-w-0 items-center gap-2"
        >
          <TypeIcon type={c.type} />
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-medium">{c.title}</span>
            <span className="block text-xs text-muted">
              {when(c)}
              {c.status !== 'open' && ` · ${c.status}`}
            </span>
          </span>
          {running && <span aria-label="running" title="running" className="h-2 w-2 animate-pulse rounded-full bg-success" />}
          {unread && <span aria-label="unread" title="unread" className="h-2 w-2 rounded-full bg-accent" />}
          <span className="text-xs tabular-nums text-muted">{cost(c.usage.costUsd)}</span>
        </Link>
      )}
      <span className="flex flex-wrap justify-end gap-1">
        <button type="button" aria-label={`Rename ${c.title}`} className={btn} onClick={() => setRenaming(true)}>
          Rename
        </button>
        {c.status === 'open' && (
          <button type="button" aria-label={`Close ${c.title}`} className={btn} onClick={() => act(() => store.getState().close(projectId, c.id))}>
            Close
          </button>
        )}
        <button
          type="button"
          aria-label={`${c.archived ? 'Unarchive' : 'Archive'} ${c.title}`}
          className={btn}
          onClick={() => act(() => store.getState().setArchived(projectId, c.id, !c.archived))}
        >
          {c.archived ? 'Unarchive' : 'Archive'}
        </button>
      </span>
    </li>
  );
}

/**
 * The project's sessions (spec §12.1): newest activity first, with running and unread marks and
 * the summed cost. A popover on desktop and iPad, a full-height sheet on the phone.
 */
export function SessionList({
  projectId,
  currentId,
  onClose,
}: {
  projectId: string;
  currentId: string | null;
  onClose: () => void;
}) {
  const store = useConversationsApi();
  const all = useConversations((s) => s.byProject[projectId] ?? EMPTY);
  const running = useConversations((s) => s.activeRun[projectId]?.conversationId ?? null);
  const unread = useConversations((s) => s.unread[projectId] ?? NONE);
  const [showArchived, setShowArchived] = useState(false);
  const [archivedError, setArchivedError] = useState<string | null>(null);
  const loadArchived = () => {
    setArchivedError(null);
    void store
      .getState()
      .loadArchived(projectId)
      .then((ok) => {
        if (!ok) setArchivedError(store.getState().errors[projectId] ?? 'unknown error');
      });
  };
  const [error, setError] = useState<string | null>(null);
  const phone = useMediaQuery(PHONE_QUERY);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const shown = all.filter((c) => showArchived || !c.archived);
  const body = (
    <>
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <h2 className="flex-1 text-sm font-semibold">Sessions</h2>
        <label className="flex items-center gap-1 text-xs text-muted">
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(e) => {
              setShowArchived(e.target.checked);
              if (e.target.checked) loadArchived();
              else setArchivedError(null);
            }}
          />
          Show archived
        </label>
        {phone && (
          <button type="button" aria-label="Close sessions" onClick={onClose} className="p-1">
            <CloseIcon className="h-5 w-5" />
          </button>
        )}
      </div>
      {error && (
        <p role="alert" className="px-3 py-1 text-xs text-danger">
          {error}
        </p>
      )}
      {showArchived && archivedError && (
        <p role="alert" className="flex items-center gap-2 px-3 py-1 text-xs text-danger">
          <span className="flex-1">Could not load the archived sessions: {archivedError}</span>
          <button type="button" onClick={loadArchived} className="rounded border border-border px-2 text-fg">
            Retry
          </button>
        </p>
      )}
      <ul aria-label="Sessions" className="flex-1 overflow-y-auto p-1">
        {shown.map((c) => (
          <Row
            key={c.id}
            projectId={projectId}
            c={c}
            current={c.id === currentId}
            running={c.id === running}
            unread={unread.includes(c.id)}
            onPick={onClose}
            onError={setError}
          />
        ))}
        {shown.length === 0 && <li className="px-2 py-3 text-sm text-muted">No sessions yet.</li>}
      </ul>
    </>
  );

  if (phone) {
    return (
      <div
        role="dialog"
        aria-label="Sessions"
        aria-modal="true"
        data-testid="session-list"
        data-variant="sheet"
        className="fixed inset-0 z-50 flex flex-col bg-bg"
        style={{ paddingTop: 'env(safe-area-inset-top)', paddingBottom: 'env(safe-area-inset-bottom)' }}
      >
        {body}
      </div>
    );
  }
  return (
    <div
      data-testid="session-list"
      data-variant="popover"
      className="absolute left-2 top-full z-40 mt-1 flex max-h-[70vh] w-[min(24rem,calc(100vw-1rem))] flex-col rounded border border-border bg-bg shadow-xl"
    >
      {body}
    </div>
  );
}
