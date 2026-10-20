import { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { SessionHeader } from '@/components/sessions/SessionHeader';
import { sessionPath } from '@/components/sessions/SessionList';
import { useChat } from '@/hooks/useChat';
import type { MessageDTO } from '@/protocol/frames';
import { chatKey } from '@/stores/chat';
import { useComposer } from '@/stores/composer';
import { useConversation, useConversations, useConversationsApi } from '@/stores/conversations';
import { usePlans } from '@/stores/plans';
import { Composer } from './Composer';
import { MessageItem, withoutDuplicateToolImages } from './MessageItem';
import { MessageList } from './MessageList';
import { RateLimitBanner } from './RateLimitBanner';
import { SubagentCard } from './SubagentCard';
import { groupSubagentRows, isDelegation, reuseGroups, type SubagentGroup } from './subagent';
import { UsageIndicator } from './UsageIndicator';

const NO_STEPS: SubagentGroup = { steps: [], result: null };

/** The title of a session of the project (falls back to "another session" while unknown). */
function useSessionTitle(projectId: string, conversationId: string | null): string | null {
  return useConversations((s) =>
    conversationId === null ? null : (s.byProject[projectId]?.find((c) => c.id === conversationId)?.title ?? 'another session'),
  );
}

/** "Plan open in <session>" while the project's open plan (loaded by ProjectPage) belongs to another session. */
function PlanElsewhere({ projectId, conversationId }: { projectId: string; conversationId: string }) {
  const owner = usePlans((s) => (s.projectId === projectId && s.open ? s.open.conversationId : null));
  const title = useSessionTitle(projectId, owner);
  if (owner === null || owner === conversationId) return null;
  return (
    <p role="note" className="border-b border-border bg-surface px-3 py-1 text-xs text-muted">
      Plan open in{' '}
      <Link to={sessionPath(projectId, owner)} className="text-accent underline">
        {title}
      </Link>
    </p>
  );
}

/** A closed session is read-only: the composer is replaced by [Reopen session] (spec §10). */
function Reopen({ projectId, conversationId }: { projectId: string; conversationId: string }) {
  const store = useConversationsApi();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function reopen() {
    setBusy(true);
    setError(null);
    try {
      await store.getState().reopen(projectId, conversationId);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div
      className="flex flex-wrap items-center gap-2 border-t border-border bg-surface px-3 pt-2 text-sm text-muted"
      style={{ paddingBottom: 'max(0.5rem, env(safe-area-inset-bottom))' }}
    >
      <span className="flex-1">This session is closed.</span>
      {error && (
        <span role="alert" className="text-danger">
          {error}
        </span>
      )}
      <button
        type="button"
        disabled={busy}
        onClick={() => void reopen()}
        className="rounded-lg border border-border px-3 py-1.5 text-fg disabled:opacity-50"
      >
        Reopen session
      </button>
    </div>
  );
}

export interface ChatPanelProps {
  projectId: string;
  /** The conversation (session) shown; its messages, composer and Stop. */
  conversationId: string;
  readOnly?: boolean;
}

export function ChatPanel({ projectId, conversationId, readOnly = false }: ChatPanelProps) {
  const chat = useChat(projectId, conversationId);
  const conversation = useConversation(projectId, conversationId);
  const status = conversation?.status ?? 'open';
  const running = useConversations((s) => s.activeRun[projectId]?.conversationId ?? null);
  const waitingFor = useSessionTitle(projectId, running !== null && running !== conversationId ? running : null);
  // The composer edits this conversation's draft (spec §12.1); before paint, so no other draft flashes.
  useLayoutEffect(() => useComposer.getState().activate(chatKey(projectId, conversationId)), [projectId, conversationId]);
  const { runActive, loadOlder } = chat;
  // Server-side queued user rows live in the composer queue, not in the history.
  const visible = useMemo(
    () => chat.messages.filter((m) => !(m.id > 0 && m.role === 'user' && m.status === 'queued')),
    [chat.messages],
  );
  // Sub-agent rows are nested under their research_part call (6A.7b); duplicate tool images dropped.
  // Unchanged groups keep their identity, so renderItem (and memoized cards/rows) stay stable.
  const prevGroups = useRef(new Map<string, SubagentGroup>());
  const { topLevel, groups } = useMemo(() => {
    const g = groupSubagentRows(visible);
    prevGroups.current = reuseGroups(prevGroups.current, g.groups);
    return { topLevel: withoutDuplicateToolImages(g.topLevel), groups: prevGroups.current };
  }, [visible]);
  const renderItem = useCallback(
    (m: MessageDTO) =>
      isDelegation(m) ? (
        <SubagentCard toolUse={m} group={groups.get(m.metadata.toolUseId as string) ?? NO_STEPS} runActive={runActive} />
      ) : (
        <MessageItem message={m} />
      ),
    [groups, runActive],
  );
  const onLoadOlder = useCallback(() => void loadOlder(), [loadOlder]);
  // Independent of lastError: dismissing the error must not remove the only way to retry.
  const canRetry = !chat.loaded && chat.loadFailed;
  const retryButton = (
    <button type="button" className="font-medium underline" onClick={chat.retry}>
      Retry
    </button>
  );

  return (
    <section aria-label="Chat" className="flex h-full min-h-0 flex-col bg-bg">
      <SessionHeader projectId={projectId} conversationId={conversationId} />
      <PlanElsewhere projectId={projectId} conversationId={conversationId} />
      <RateLimitBanner until={chat.rateLimitedUntil} />
      {chat.lastError && (
        <div role="alert" className="flex items-start gap-2 border-b border-danger bg-danger/10 px-3 py-2 text-sm text-danger">
          <span className="flex-1">{chat.lastError}</span>
          {canRetry && retryButton}
          <button type="button" aria-label="Dismiss error" onClick={chat.dismissError}>
            ✕
          </button>
        </div>
      )}
      {!chat.lastError && canRetry && (
        <div className="flex items-start gap-2 border-b border-border bg-surface px-3 py-2 text-sm text-muted">
          <span className="flex-1">Messages are not loaded.</span>
          {retryButton}
        </div>
      )}
      {readOnly && (
        <p className="border-b border-border bg-surface px-3 py-1 text-xs text-muted">
          Read-only: this project is open in KiCad — close it there to let CopperChat save.
        </p>
      )}
      <MessageList
        messages={topLevel}
        hasMoreOlder={chat.hasMoreOlder}
        loadingOlder={chat.loadingOlder}
        onLoadOlder={onLoadOlder}
        renderItem={renderItem}
      />
      <UsageIndicator usage={chat.usage} />
      {status === 'closed' ? (
        <Reopen projectId={projectId} conversationId={conversationId} />
      ) : (
        <Composer
          projectId={projectId}
          runActive={runActive}
          queued={chat.queued}
          // `send` is refused unless the session is open (closing: the handover turn runs).
          disabled={!chat.loaded || status !== 'open'}
          waitingFor={waitingFor}
          onSend={chat.send}
          onStop={chat.stop}
          onCancelQueued={chat.cancelQueued}
        />
      )}
    </section>
  );
}
