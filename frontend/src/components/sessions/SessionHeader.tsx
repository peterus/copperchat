import { ChevronDown, Pencil, Plus, Square } from 'lucide-react';
import { useState } from 'react';
import type { ConversationStatus } from '@/protocol/frames';
import { useConversation, useConversations, useConversationsApi } from '@/stores/conversations';
import { NewSessionDialog } from './NewSessionDialog';
import { SessionList } from './SessionList';
import { TitleEdit } from './TitleEdit';
import { TypeIcon } from './TypeIcon';

const STATUS_CLASS: Record<ConversationStatus, string> = {
  open: 'bg-success/15 text-success',
  closing: 'bg-warning/15 text-warning',
  closed: 'bg-surface-2 text-muted',
};

/** The running session's title (another session of the project), or null. */
function useRunningElsewhere(projectId: string, conversationId: string): string | null {
  const running = useConversations((s) => s.activeRun[projectId]?.conversationId ?? null);
  const title = useConversations((s) =>
    running === null ? null : (s.byProject[projectId]?.find((c) => c.id === running)?.title ?? 'Another session'),
  );
  return running !== null && running !== conversationId ? title : null;
}

/**
 * Top of the chat (spec §12.1): type icon, inline-renamable title, status chip, the
 * "<session> running" chip with Stop, the session list (▾), [New session] and [Close session].
 */
export function SessionHeader({ projectId, conversationId }: { projectId: string; conversationId: string }) {
  const store = useConversationsApi();
  const c = useConversation(projectId, conversationId);
  const runningElsewhere = useRunningElsewhere(projectId, conversationId);
  const [renaming, setRenaming] = useState(false);
  const [listOpen, setListOpen] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [closing, setClosing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!c) return null;

  async function close() {
    setClosing(true);
    setError(null);
    try {
      await store.getState().close(projectId, conversationId);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setClosing(false);
    }
  }

  const btn = 'flex shrink-0 items-center gap-1 rounded border border-border px-2 py-0.5 text-xs disabled:opacity-50';
  return (
    <div className="relative border-b border-border bg-surface px-3 py-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <TypeIcon type={c.type} />
        {renaming ? (
          <TitleEdit
            initial={c.title}
            onCancel={() => setRenaming(false)}
            onSave={async (title) => {
              await store.getState().rename(projectId, conversationId, title);
              setRenaming(false);
            }}
          />
        ) : (
          <>
            <h2 className="min-w-0 truncate text-sm font-semibold" title={c.title}>
              {c.title}
            </h2>
            <button type="button" aria-label="Rename session" onClick={() => setRenaming(true)} className="p-0.5 text-muted">
              <Pencil className="h-3.5 w-3.5" />
            </button>
          </>
        )}
        <span aria-label="Session status" className={`rounded px-1.5 text-xs ${STATUS_CLASS[c.status]}`}>
          {c.status}
        </span>
        <button
          type="button"
          aria-label="Sessions"
          aria-expanded={listOpen}
          onClick={() => setListOpen((o) => !o)}
          className="p-0.5 text-muted"
        >
          <ChevronDown className="h-4 w-4" />
        </button>
        <span className="flex-1" />
        {runningElsewhere !== null && (
          <span className="flex items-center gap-1 rounded bg-warning/15 px-1.5 py-0.5 text-xs">
            <span className="h-2 w-2 animate-pulse rounded-full bg-success" aria-hidden />
            {`${runningElsewhere} running`}
            <button
              type="button"
              aria-label={`Stop ${runningElsewhere}`}
              onClick={() => store.getState().interrupt(projectId)}
              className="ml-1 flex items-center gap-0.5 rounded border border-border px-1 text-danger"
            >
              <Square className="h-3 w-3" /> Stop
            </button>
          </span>
        )}
        <button type="button" onClick={() => setDialogOpen(true)} className={btn}>
          <Plus className="h-3.5 w-3.5" /> New session
        </button>
        {c.status === 'open' && (
          <button type="button" disabled={closing} onClick={() => void close()} className={btn}>
            Close session
          </button>
        )}
      </div>
      {error && (
        <p role="alert" className="mt-1 flex items-start gap-2 text-xs text-danger">
          <span className="flex-1">{error}</span>
          <button type="button" aria-label="Dismiss" onClick={() => setError(null)}>
            ✕
          </button>
        </p>
      )}
      {listOpen && <SessionList projectId={projectId} currentId={conversationId} onClose={() => setListOpen(false)} />}
      {dialogOpen && <NewSessionDialog projectId={projectId} onClose={() => setDialogOpen(false)} />}
    </div>
  );
}
