import { useCallback, useMemo } from 'react';
import { useChat } from '@/hooks/useChat';
import type { MessageDTO } from '@/protocol/frames';
import { Composer } from './Composer';
import { MessageItem, withoutDuplicateToolImages } from './MessageItem';
import { MessageList } from './MessageList';
import { RateLimitBanner } from './RateLimitBanner';
import { SubagentCard } from './SubagentCard';
import { groupSubagentRows, isDelegation, type SubagentGroup } from './subagent';
import { UsageIndicator } from './UsageIndicator';

const NO_STEPS: SubagentGroup = { steps: [], result: null };

export interface ChatPanelProps {
  projectId: string;
  readOnly?: boolean;
}

export function ChatPanel({ projectId, readOnly = false }: ChatPanelProps) {
  const chat = useChat(projectId);
  const { runActive, loadOlder } = chat;
  // Server-side queued user rows live in the composer queue, not in the history.
  const visible = useMemo(
    () => chat.messages.filter((m) => !(m.id > 0 && m.role === 'user' && m.status === 'queued')),
    [chat.messages],
  );
  // Sub-agent rows are nested under their research_part call (6A.7b); duplicate tool images dropped.
  const { topLevel, groups } = useMemo(() => {
    const g = groupSubagentRows(visible);
    return { topLevel: withoutDuplicateToolImages(g.topLevel), groups: g.groups };
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

  return (
    <section aria-label="Chat" className="flex h-full min-h-0 flex-col bg-bg">
      <RateLimitBanner until={chat.rateLimitedUntil} />
      {chat.lastError && (
        <div role="alert" className="flex items-start gap-2 border-b border-danger bg-danger/10 px-3 py-2 text-sm text-danger">
          <span className="flex-1">{chat.lastError}</span>
          <button type="button" aria-label="Dismiss error" onClick={chat.dismissError}>
            ✕
          </button>
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
      <Composer
        projectId={projectId}
        runActive={runActive}
        queued={chat.queued}
        disabled={!chat.loaded}
        onSend={chat.send}
        onStop={chat.stop}
        onCancelQueued={chat.cancelQueued}
      />
    </section>
  );
}
