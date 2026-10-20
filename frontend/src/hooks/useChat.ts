import { createContext, useCallback, useContext, useEffect, useMemo, useRef } from 'react';
import type { AttachmentRef, MessageDTO, Usage } from '@/protocol/frames';
import { chatKey, emptyChat, useChatStore, type ActiveRun, type ChatStore } from '@/stores/chat';

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

/** The project's running agent run (any conversation), or null. */
export function useActiveRun(projectId: string): ActiveRun | null {
  const useStore = useContext(ChatStoreContext);
  return useStore((s) => s.activeRuns[projectId]) ?? null;
}

/** The conversation the project page shows (the last one opened), or null before it is known. */
export function useCurrentConversationId(projectId: string): string | null {
  const useStore = useContext(ChatStoreContext);
  return useStore((s) => s.current[projectId]) ?? null;
}

/**
 * The chat of one conversation (S-C8). `conversationId` null (not known yet): an empty chat
 * whose send() refuses; nothing is loaded.
 */
export function useChat(projectId: string, conversationId: string | null): UseChatResult {
  const useStore = useContext(ChatStoreContext);
  const chat = useStore((s) => (conversationId === null ? undefined : s.chats[chatKey(projectId, conversationId)])) ?? EMPTY;

  useEffect(() => {
    // open() also calls ws.ensure(): login does not connect the socket (L3).
    if (conversationId !== null) void useStore.getState().open(projectId, conversationId);
  }, [useStore, projectId, conversationId]);

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
    (text: string, attachments?: AttachmentRef[]) =>
      conversationId !== null && useStore.getState().send(projectId, conversationId, text, attachments),
    [useStore, projectId, conversationId],
  );
  const cancelQueued = useCallback(
    (id: number) => {
      if (conversationId !== null) useStore.getState().cancelQueued(projectId, conversationId, id);
    },
    [useStore, projectId, conversationId],
  );
  const stop = useCallback(() => {
    if (conversationId !== null) useStore.getState().stop(projectId, conversationId);
  }, [useStore, projectId, conversationId]);
  const loadOlder = useCallback(
    () => (conversationId !== null ? useStore.getState().loadOlder(projectId, conversationId) : Promise.resolve()),
    [useStore, projectId, conversationId],
  );
  const dismissError = useCallback(() => {
    if (conversationId !== null) useStore.getState().dismissError(projectId, conversationId);
  }, [useStore, projectId, conversationId]);
  const retry = useCallback(() => {
    if (conversationId !== null) void useStore.getState().open(projectId, conversationId);
  }, [useStore, projectId, conversationId]);

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
