import { createContext, useCallback, useContext, useEffect, useMemo, useRef } from 'react';
import type { AttachmentRef, MessageDTO, Usage } from '@/protocol/frames';
import { emptyChat, useChatStore, type ChatStore } from '@/stores/chat';

/** The app uses the singleton store; tests inject a store backed by FakeWsClient. */
export const ChatStoreContext = createContext<ChatStore>(useChatStore);

const EMPTY = emptyChat();

export interface UseChatResult {
  messages: MessageDTO[];
  runActive: boolean;
  currentRunId: string | null;
  /** Server-acknowledged user rows still waiting for the agent (cancellable). */
  queued: MessageDTO[];
  /** False when the message was refused locally (see lastError); the caller keeps the draft. */
  send: (text: string, attachments?: AttachmentRef[]) => boolean;
  cancelQueued: (id: number) => void;
  stop: () => void;
  loadOlder: () => Promise<void>;
  hasMoreOlder: boolean;
  loadingOlder: boolean;
  loaded: boolean;
  /** The initial load failed and none is in flight: offer retry(). */
  loadFailed: boolean;
  usage: Usage | null;
  rateLimitedUntil: string | null;
  lastError: string | null;
  dismissError: () => void;
  /** Re-run a failed initial load. */
  retry: () => void;
}

export function useChat(projectId: string): UseChatResult {
  const useStore = useContext(ChatStoreContext);
  const chat = useStore((s) => s.chats[projectId]) ?? EMPTY;

  useEffect(() => {
    // open() also calls ws.ensure(): login does not connect the socket (L3).
    void useStore.getState().open(projectId);
  }, [useStore, projectId]);

  // Same rows (by identity) → same array, so the Composer does not re-render on every delta.
  const prevQueued = useRef<MessageDTO[]>([]);
  const queued = useMemo(() => {
    const next = chat.messages.filter((m) => m.id > 0 && m.role === 'user' && m.status === 'queued');
    const prev = prevQueued.current;
    if (next.length === prev.length && next.every((m, i) => m === prev[i])) return prev;
    prevQueued.current = next;
    return next;
  }, [chat.messages]);
  const send = useCallback(
    (text: string, attachments?: AttachmentRef[]) => useStore.getState().send(projectId, text, attachments),
    [useStore, projectId],
  );
  const cancelQueued = useCallback((id: number) => useStore.getState().cancelQueued(projectId, id), [useStore, projectId]);
  const stop = useCallback(() => useStore.getState().stop(projectId), [useStore, projectId]);
  const loadOlder = useCallback(() => useStore.getState().loadOlder(projectId), [useStore, projectId]);
  const dismissError = useCallback(() => useStore.getState().dismissError(projectId), [useStore, projectId]);
  const retry = useCallback(() => void useStore.getState().open(projectId), [useStore, projectId]);

  return {
    messages: chat.messages,
    runActive: chat.runActive,
    currentRunId: chat.currentRunId,
    queued,
    send,
    cancelQueued,
    stop,
    loadOlder,
    hasMoreOlder: chat.hasMoreOlder,
    loadingOlder: chat.loadingOlder,
    loaded: chat.loaded,
    loadFailed: chat.loadFailed,
    usage: chat.usage,
    rateLimitedUntil: chat.rateLimitedUntil,
    lastError: chat.lastError,
    dismissError,
    retry,
  };
}
