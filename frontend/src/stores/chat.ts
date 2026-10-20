import { z } from 'zod';
import { create } from 'zustand';
import { fetchConversationMessages } from '@/api/conversations';
import { wsClient, type ConnectionState, type WsClient } from '@/api/ws';
import {
  AttachmentRef,
  ClientFrame,
  type MessagesPage,
  type MessageDeltaFrame,
  type MessageDTO,
  type ServerFrame,
  type Usage,
} from '@/protocol/frames';

export const PAGE_SIZE = 50;
const TERMINAL_STATUSES: ReadonlySet<string> = new Set(['done', 'cancelled', 'error']);

/** Store key of one conversation's chat (S-C8). Both ids are hex, so the slash is unambiguous. */
export function chatKey(projectId: string, conversationId: string): string {
  return `${projectId}/${conversationId}`;
}

function splitKey(key: string): [projectId: string, conversationId: string] {
  const i = key.indexOf('/');
  return [key.slice(0, i), key.slice(i + 1)];
}

/** The project's running agent run. One burst runs at a time per project (FIFO across conversations). */
export interface ActiveRun {
  conversationId: string;
  runId: string;
}

/** The chat of one conversation. `runActive` / `currentRunId`: a run of THIS conversation is active. */
export interface ConversationChat {
  messages: MessageDTO[];
  lastSeenId: number;
  oldestId: number | null;
  hasMoreOlder: boolean;
  loadingOlder: boolean;
  loaded: boolean;
  /** The last initial load failed and none is in flight (Retry stays offered after a dismiss). */
  loadFailed: boolean;
  runActive: boolean;
  currentRunId: string | null;
  usage: Usage | null;
  rateLimitedUntil: string | null;
  lastError: string | null;
  /** Rows that may have missed deltas (reconnect / unknown-id delta); re-healed when they settle. */
  suspectIds: number[];
}

export function emptyChat(): ConversationChat {
  return {
    messages: [],
    lastSeenId: 0,
    oldestId: null,
    hasMoreOlder: false,
    loadingOlder: false,
    loaded: false,
    loadFailed: false,
    runActive: false,
    currentRunId: null,
    usage: null,
    rateLimitedUntil: null,
    lastError: null,
    suspectIds: [],
  };
}

export interface ChatDeps {
  ws: WsClient;
  fetchPage: (projectId: string, conversationId: string, opts: { limit: number; before?: number }) => Promise<MessagesPage>;
  newClientMessageId?: () => string;
}

export interface ChatState {
  /** Keyed by chatKey(projectId, conversationId). */
  chats: Readonly<Record<string, ConversationChat>>;
  /** Per project: the run in progress (run-started until run-finished), whichever conversation it belongs to. */
  activeRuns: Readonly<Record<string, ActiveRun>>;
  /**
   * Per project: the conversation shown (the last one opened). The WS subscription is per project
   * (live frames of all its conversations arrive), but the replay cursor follows this conversation.
   */
  current: Readonly<Record<string, string>>;
  /** Loads the conversation once and makes it the project's current one (its replay is subscribed). */
  open: (projectId: string, conversationId: string) => Promise<void>;
  loadOlder: (projectId: string, conversationId: string) => Promise<void>;
  /** False (and lastError set) when the frame would be refused by the protocol; nothing is queued then. */
  send: (projectId: string, conversationId: string, text: string, attachments?: AttachmentRef[]) => boolean;
  cancelQueued: (projectId: string, conversationId: string, messageId: number) => void;
  /** Interrupts the project's active run (with that run's conversation); without one, this conversation. */
  stop: (projectId: string, conversationId: string) => void;
  heal: (projectId: string, conversationId: string) => Promise<void>;
  dismissError: (projectId: string, conversationId: string) => void;
  /** Drops every chat (logout / 4401); results of in-flight requests are discarded. */
  reset: () => void;
}

/** GET …/conversations/{cid}/messages: the newest `limit` rows below `before`, ascending by id. */
export const fetchMessagesPage = fetchConversationMessages;


/** crypto.randomUUID only exists in secure contexts; the app may be served over plain http on a LAN. */
export function randomClientMessageId(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

/** Server rows ascending by id, then optimistic rows (negative ids) in creation order. */
export function compareMessages(a: MessageDTO, b: MessageDTO): number {
  const aOpt = a.id < 0;
  const bOpt = b.id < 0;
  if (aOpt !== bOpt) return aOpt ? 1 : -1;
  return aOpt ? b.id - a.id : a.id - b.id;
}

function sameRow(a: MessageDTO, b: MessageDTO): boolean {
  return (
    a.content === b.content &&
    a.status === b.status &&
    a.runId === b.runId &&
    a.createdAt === b.createdAt &&
    a.role === b.role &&
    a.kind === b.kind &&
    a.clientMessageId === b.clientMessageId &&
    (a.metadata === b.metadata || JSON.stringify(a.metadata) === JSON.stringify(b.metadata))
  );
}

/**
 * Server snapshot vs local copy of the same row (DB content lags the live stream). An unchanged
 * row keeps its identity so memoized rows do not re-render on every heal.
 */
export function mergeRow(existing: MessageDTO | undefined, incoming: MessageDTO): MessageDTO {
  if (!existing) return incoming;
  let merged = incoming;
  if (incoming.status === 'streaming' && existing.content.startsWith(incoming.content)) {
    if (TERMINAL_STATUSES.has(existing.status)) return existing; // stale snapshot of a finished row
    if (existing.content.length > incoming.content.length) merged = { ...incoming, content: existing.content };
  }
  return sameRow(existing, merged) ? existing : merged;
}

/** Merge by id (one row per id); a server row replaces the optimistic row with its clientMessageId. */
export function mergeRows(list: MessageDTO[], rows: MessageDTO[]): MessageDTO[] {
  const byId = new Map(list.map((m) => [m.id, m] as const));
  for (const r of rows) {
    if (r.clientMessageId !== null) {
      for (const [id, m] of byId) {
        if (id < 0 && m.clientMessageId === r.clientMessageId) byId.delete(id);
      }
    }
    byId.set(r.id, mergeRow(byId.get(r.id), r));
  }
  return [...byId.values()].sort(compareMessages);
}

function maxServerId(rows: MessageDTO[]): number {
  return rows.reduce((max, r) => (r.id > max ? r.id : max), 0);
}

function minServerId(rows: MessageDTO[]): number | null {
  let min: number | null = null;
  for (const r of rows) if (r.id > 0 && (min === null || r.id < min)) min = r.id;
  return min;
}

function applyDelta(m: MessageDTO, f: MessageDeltaFrame): MessageDTO {
  return {
    ...m,
    content: f.contentDelta !== undefined ? m.content + f.contentDelta : m.content,
    status: f.statusPatch ?? m.status,
    metadata: f.metadataPatch ? { ...m.metadata, ...f.metadataPatch } : m.metadata,
  };
}

/**
 * Rows of a baseline/heal page that are still streaming: the GET overlays the live content, but the
 * deltas between the GET and the (re)subscribe are never replayed (replay only sends id > since).
 * They become suspects and are re-healed once they reach a terminal status.
 */
function withStreamingSuspects(c: ConversationChat, page: MessageDTO[]): ConversationChat {
  const streaming = page.filter((r) => r.id > 0 && r.status === 'streaming' && !c.suspectIds.includes(r.id));
  if (streaming.length === 0) return c;
  return { ...c, suspectIds: [...c.suspectIds, ...streaming.map((r) => r.id)] };
}

function frameIssue(frame: ClientFrame): string | null {
  const parsed = ClientFrame.safeParse(frame);
  return parsed.success ? null : parsed.error.issues.map((i) => i.message).join('; ');
}

function attachmentsOf(m: MessageDTO): AttachmentRef[] {
  const parsed = z.array(AttachmentRef).safeParse(m.metadata.attachments);
  return parsed.success ? parsed.data : [];
}

const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));


export function createChatStore(deps: ChatDeps) {
  // All maps below are keyed by chatKey(projectId, conversationId).
  const opening = new Map<string, Promise<void>>();
  const healing = new Map<string, { running: Promise<void>; again: boolean }>();
  // Projects subscribed on the current socket (a second subscribe replaces the first).
  const subscribedOnSocket = new Set<string>();
  // Bumped by reset(): an HTTP response that started in an older generation is ignored.
  let generation = 0;
  let optimisticSeq = 0;
  const newClientMessageId = deps.newClientMessageId ?? randomClientMessageId;

  return create<ChatState>()((set, get) => {
    function patch(key: string, fn: (c: ConversationChat) => ConversationChat): void {
      const current = get().chats[key];
      if (!current) return;
      const next = fn(current);
      if (next !== current) set({ chats: { ...get().chats, [key]: next } });
    }

    /** Keys of the project's chats (loaded or loading). */
    function keysOf(projectId: string): string[] {
      const prefix = `${projectId}/`;
      return Object.keys(get().chats).filter((k) => k.startsWith(prefix));
    }

    function withRows(c: ConversationChat, rows: MessageDTO[]): ConversationChat {
      return { ...c, messages: mergeRows(c.messages, rows), lastSeenId: Math.max(c.lastSeenId, maxServerId(rows)) };
    }

    /**
     * Subscribes the project's current conversation; only with an open socket (returns false
     * otherwise), the connection handler subscribes on open.
     */
    function subscribe(projectId: string): boolean {
      const conversationId = get().current[projectId];
      if (conversationId === undefined) return false;
      const c = get().chats[chatKey(projectId, conversationId)];
      if (!c?.loaded || !deps.ws.isOpen()) return false;
      // Re-subscribing replaces the server-side replay cursor (spec §6.2); there is no unsubscribe.
      deps.ws.send({ type: 'subscribe', projectId, conversationId, sinceMessageId: c.lastSeenId });
      if (subscribedOnSocket.has(projectId)) afterResubscribe(projectId);
      subscribedOnSocket.add(projectId);
      return true;
    }

    /**
     * The server drops the project's old subscription before the new replay, so live frames of
     * the project's other conversations may be lost in between (a run-finished, deltas): like a
     * reconnect, forget the active run (re-sent on subscribe while it is still active) and heal
     * the project's loaded chats over HTTP.
     */
    function afterResubscribe(projectId: string): void {
      if (get().activeRuns[projectId]) {
        const { [projectId]: _gone, ...rest } = get().activeRuns;
        set({ activeRuns: rest });
      }
      for (const [key, chat] of Object.entries(get().chats)) {
        if (!chat.loaded || splitKey(key)[0] !== projectId) continue;
        patch(key, markDisconnected);
        void healKey(key);
      }
    }

    /** Server dedupes on clientMessageId, so retransmitting is always safe. */
    function resendPending(key: string): void {
      const c = get().chats[key];
      if (!c) return;
      const [projectId, conversationId] = splitKey(key);
      for (const m of c.messages) {
        if (m.id >= 0 || m.status === 'error' || m.clientMessageId === null) continue;
        try {
          deps.ws.send({
            type: 'send',
            projectId,
            conversationId,
            clientMessageId: m.clientMessageId,
            text: m.content,
            attachments: attachmentsOf(m),
          });
        } catch (e) {
          // An unsendable row must not break every reconnect: mark it failed instead.
          const error = `not sendable: ${errorText(e)}`;
          patch(key, (cur) => ({
            ...cur,
            messages: cur.messages.map((x) =>
              x.id === m.id ? { ...x, status: 'error' as const, metadata: { ...x.metadata, error } } : x,
            ),
          }));
        }
      }
    }

    function healKey(key: string): Promise<void> {
      const [projectId, conversationId] = splitKey(key);
      return get().heal(projectId, conversationId);
    }

    function onDelta(key: string, f: MessageDeltaFrame): void {
      const c = get().chats[key];
      if (!c) return;
      const existing = c.messages.find((m) => m.id === f.messageId);
      if (!existing) {
        // Its `message` frame was lost (e.g. mid-reconnect): heal instead of dropping.
        patch(key, (cur) => ({
          ...cur,
          suspectIds: cur.suspectIds.includes(f.messageId) ? cur.suspectIds : [...cur.suspectIds, f.messageId],
        }));
        void healKey(key);
        return;
      }
      const updated = applyDelta(existing, f);
      const settledSuspect =
        c.suspectIds.includes(f.messageId) && f.statusPatch !== undefined && TERMINAL_STATUSES.has(f.statusPatch);
      patch(key, (cur) => ({
        ...cur,
        messages: cur.messages.map((m) => (m.id === updated.id ? updated : m)),
        suspectIds: settledSuspect ? cur.suspectIds.filter((id) => id !== f.messageId) : cur.suspectIds,
      }));
      // A suspect may have lost deltas; once final, the DB row is authoritative.
      if (settledSuspect) void healKey(key);
    }

    /** Frames may have been lost with the socket: a run-finished (stuck Stop) or deltas. */
    function markDisconnected(c: ConversationChat): ConversationChat {
      const streaming = c.messages.filter((m) => m.id > 0 && m.status === 'streaming').map((m) => m.id);
      const suspectIds = [...new Set([...c.suspectIds, ...streaming])];
      if (!c.runActive && c.currentRunId === null && suspectIds.length === c.suspectIds.length) return c;
      return { ...c, runActive: false, currentRunId: null, suspectIds };
    }

    function onConnection(state: ConnectionState): void {
      subscribedOnSocket.clear(); // a new socket has no subscriptions
      // The server re-sends run-started on subscribe while the run is still active, so
      // resetting here never hides a live run for longer than the replay.
      if (Object.keys(get().activeRuns).length > 0) set({ activeRuns: {} });
      const live = state === 'open' || state === 'resumed';
      const subscribed = new Set<string>();
      for (const [key, chat] of Object.entries(get().chats)) {
        const [projectId, conversationId] = splitKey(key);
        if (!chat.loaded) {
          // An in-flight initial load subscribes itself with the right lastSeenId; a failed one is retried.
          if (live && !opening.has(key)) void startLoad(projectId, conversationId);
          continue;
        }
        patch(key, markDisconnected);
        // closed / unauthorized / forbidden: nothing to talk to; 'open' follows on reconnect.
        if (!live) continue;
        // One subscription per project: the current conversation's replay (others heal over HTTP).
        if (!subscribed.has(projectId) && get().current[projectId] === conversationId) {
          subscribed.add(projectId);
          subscribe(projectId);
        }
        resendPending(key);
        void healKey(key); // half-streamed rows: subscribe(since) never re-sends rows <= lastSeenId
      }
    }

    function onRunFinished(projectId: string, f: Extract<ServerFrame, { type: 'run-finished' }>): void {
      const active = get().activeRuns[projectId];
      if (active && active.runId === f.runId) {
        const { [projectId]: _done, ...rest } = get().activeRuns;
        set({ activeRuns: rest });
      }
      const key = chatKey(projectId, f.conversationId);
      const c = get().chats[key];
      if (!c) return;
      // A suspect's terminal delta may have been lost (it is sent once, never replayed): heal.
      if (c.suspectIds.length > 0) void healKey(key);
      patch(key, (cur) => {
        if (cur.currentRunId !== null && cur.currentRunId !== f.runId) return cur; // late finish of an older run
        return {
          ...cur,
          runActive: false,
          currentRunId: null,
          lastError: f.reason === 'error' ? (cur.lastError ?? 'The agent run ended with an error.') : cur.lastError,
        };
      });
    }

    function onError(projectId: string, f: Extract<ServerFrame, { type: 'error' }>): void {
      const keys = keysOf(projectId);
      // cancelQueued marked the row cancelled optimistically; the server row is authoritative.
      if (f.code === 'not_cancellable') for (const key of keys) void healKey(key);
      // The chat that sent the refused message, else the conversation shown.
      const owner = f.clientMessageId
        ? keys.find((k) => get().chats[k]!.messages.some((m) => m.id < 0 && m.clientMessageId === f.clientMessageId))
        : undefined;
      const currentId = get().current[projectId];
      const key = owner ?? (currentId !== undefined ? chatKey(projectId, currentId) : undefined);
      if (key === undefined) return;
      patch(key, (c) => ({
        ...c,
        lastError: f.message,
        messages: f.clientMessageId
          ? c.messages.map((m) =>
              m.id < 0 && m.clientMessageId === f.clientMessageId
                ? { ...m, status: 'error' as const, metadata: { ...m.metadata, error: f.message } }
                : m,
            )
          : c.messages,
      }));
    }

    function onFrame(f: ServerFrame): void {
      if (f.type === 'error' && f.projectId === null) {
        for (const key of Object.keys(get().chats)) patch(key, (c) => ({ ...c, lastError: f.message }));
        return;
      }
      // Frames of projects that were never opened are ignored; within a project they are routed
      // by conversationId (frames of conversations that are not loaded only touch activeRuns).
      if (f.projectId === null || get().current[f.projectId] === undefined) return;
      const projectId = f.projectId;
      switch (f.type) {
        case 'message':
          patch(chatKey(projectId, f.message.conversationId), (c) => withRows(c, [f.message]));
          return;
        case 'message-delta':
          onDelta(chatKey(projectId, f.conversationId), f);
          return;
        case 'run-started':
          set({ activeRuns: { ...get().activeRuns, [projectId]: { conversationId: f.conversationId, runId: f.runId } } });
          patch(chatKey(projectId, f.conversationId), (c) => ({
            ...c,
            runActive: true,
            currentRunId: f.runId,
            rateLimitedUntil: null,
            lastError: null,
          }));
          return;
        case 'run-finished':
          onRunFinished(projectId, f);
          return;
        case 'usage':
          patch(chatKey(projectId, f.conversationId), (c) => ({ ...c, usage: f.usage }));
          return;
        case 'rate-limited':
          // The account limit holds for every conversation of the project.
          for (const key of keysOf(projectId)) patch(key, (c) => ({ ...c, rateLimitedUntil: f.resetsAt }));
          return;
        case 'error':
          onError(projectId, f);
          return;
        case 'view-updated': // consumed by the viewer (Phase 7); ping/pong never reach here (projectId null)
        case 'changes-updated': // consumed by the Changes panel (E9a)
        case 'plan-updated': // consumed by the plan store (E9b)
        case 'conversation-updated': // consumed by the session list (C2)
          return;
      }
    }

    async function load(projectId: string, conversationId: string): Promise<void> {
      const key = chatKey(projectId, conversationId);
      if (!get().chats[key]) set({ chats: { ...get().chats, [key]: emptyChat() } });
      else patch(key, (c) => (c.loadFailed ? { ...c, loadFailed: false } : c));
      const gen = generation;
      let streaming = false;
      try {
        const page = await deps.fetchPage(projectId, conversationId, { limit: PAGE_SIZE });
        if (gen !== generation) return;
        streaming = page.messages.some((r) => r.id > 0 && r.status === 'streaming');
        patch(key, (c) => {
          const next = withStreamingSuspects(withRows(c, page.messages), page.messages);
          return { ...next, loaded: true, hasMoreOlder: page.hasMore, oldestId: minServerId(next.messages), lastError: null };
        });
      } catch (e) {
        if (gen === generation) patch(key, (c) => ({ ...c, loadFailed: true, lastError: `Could not load messages: ${errorText(e)}` }));
        return;
      }
      // A suspect's terminal delta sent between the GET and the subscribe is lost (replay only sends
      // id > since): re-read the DB once subscribed. Without an open socket, 'open' heals anyway.
      // A conversation that is no longer current gets no replay: heal it the same way.
      const subscribed = get().current[projectId] === conversationId ? subscribe(projectId) : deps.ws.isOpen();
      if (subscribed && streaming) await get().heal(projectId, conversationId);
    }

    /** One in-flight load per conversation; does not change the current conversation. */
    function startLoad(projectId: string, conversationId: string): Promise<void> {
      const key = chatKey(projectId, conversationId);
      const inflight = opening.get(key);
      if (inflight) return inflight;
      const p = load(projectId, conversationId).finally(() => {
        if (opening.get(key) === p) opening.delete(key);
      });
      opening.set(key, p);
      return p;
    }

    async function healOnce(projectId: string, conversationId: string): Promise<void> {
      const key = chatKey(projectId, conversationId);
      const gen = generation;
      try {
        const page = await deps.fetchPage(projectId, conversationId, { limit: PAGE_SIZE });
        if (gen !== generation) return;
        patch(key, (c) => {
          // Rows still streaming after the merge may have adopted DB text that in-flight deltas repeat.
          const next = withStreamingSuspects(withRows(c, page.messages), page.messages);
          const settled = new Set(next.messages.filter((m) => TERMINAL_STATUSES.has(m.status)).map((m) => m.id));
          return {
            ...next,
            oldestId: minServerId(next.messages),
            hasMoreOlder: c.oldestId === null ? page.hasMore : c.hasMoreOlder,
            suspectIds: next.suspectIds.filter((id) => !settled.has(id)),
          };
        });
      } catch (e) {
        console.warn('[chat] heal failed', e);
      }
    }

    deps.ws.onFrame(onFrame);
    deps.ws.onConnection(onConnection);

    return {
      chats: {},
      activeRuns: {},
      current: {},

      open: (projectId, conversationId) => {
        // Always (re)enable the socket: after a logout/4401 the client stays down until ensure().
        deps.ws.ensure();
        const switched = get().current[projectId] !== conversationId;
        if (switched) set({ current: { ...get().current, [projectId]: conversationId } });
        if (get().chats[chatKey(projectId, conversationId)]?.loaded) {
          // Moving the replay cursor to an already-loaded conversation re-subscribes it.
          if (switched) subscribe(projectId);
          return Promise.resolve();
        }
        return startLoad(projectId, conversationId);
      },

      loadOlder: async (projectId, conversationId) => {
        const key = chatKey(projectId, conversationId);
        const c = get().chats[key];
        if (!c?.loaded || c.loadingOlder || !c.hasMoreOlder || c.oldestId === null) return;
        const before = c.oldestId;
        patch(key, (cur) => ({ ...cur, loadingOlder: true }));
        const gen = generation;
        try {
          const page = await deps.fetchPage(projectId, conversationId, { limit: PAGE_SIZE, before });
          if (gen !== generation) return;
          patch(key, (cur) => {
            const messages = mergeRows(cur.messages, page.messages);
            return { ...cur, messages, oldestId: minServerId(messages), hasMoreOlder: page.hasMore, loadingOlder: false };
          });
        } catch (e) {
          if (gen !== generation) return;
          patch(key, (cur) => ({ ...cur, loadingOlder: false, lastError: `Could not load older messages: ${errorText(e)}` }));
        }
      },

      send: (projectId, conversationId, text, attachments = []) => {
        const key = chatKey(projectId, conversationId);
        if (!get().chats[key] || (text.trim() === '' && attachments.length === 0)) return false;
        const clientMessageId = newClientMessageId();
        const frame: ClientFrame = { type: 'send', projectId, conversationId, clientMessageId, text, attachments };
        // Validate before the optimistic insert: a row the protocol refuses would be stuck forever.
        const issue = frameIssue(frame);
        if (issue !== null) {
          patch(key, (c) => ({ ...c, lastError: `Message not sent: ${issue}` }));
          return false;
        }
        optimisticSeq += 1;
        const optimistic: MessageDTO = {
          id: -optimisticSeq,
          projectId,
          conversationId,
          clientMessageId,
          runId: null,
          role: 'user',
          kind: 'text',
          content: text,
          status: 'queued',
          metadata: { pending: true, attachments },
          createdAt: new Date().toISOString(),
        };
        patch(key, (c) => ({ ...c, messages: [...c.messages, optimistic].sort(compareMessages) }));
        // Not open: resendPending() sends it on 'open' (sending now would queue a duplicate).
        if (deps.ws.isOpen()) deps.ws.send(frame);
        return true;
      },

      cancelQueued: (projectId, conversationId, messageId) => {
        if (messageId <= 0) return;
        deps.ws.send({ type: 'cancel-queued', projectId, messageId });
        patch(chatKey(projectId, conversationId), (c) => ({
          ...c,
          messages: c.messages.map((m) => (m.id === messageId ? { ...m, status: 'cancelled' as const } : m)),
        }));
      },

      stop: (projectId, conversationId) => {
        const c = get().chats[chatKey(projectId, conversationId)];
        // The Stop targets the running run, which may belong to another conversation of the project.
        const active = get().activeRuns[projectId];
        if (!c && !active) return;
        deps.ws.send({
          type: 'interrupt',
          projectId,
          conversationId: active?.conversationId ?? conversationId,
          runId: active?.runId ?? c?.currentRunId ?? null,
        });
      },

      heal: (projectId, conversationId) => {
        const key = chatKey(projectId, conversationId);
        const state = healing.get(key);
        if (state) {
          state.again = true; // a newer request arrived mid-fetch: fetch once more afterwards
          return state.running;
        }
        const entry = { running: Promise.resolve(), again: false };
        entry.running = (async () => {
          try {
            do {
              entry.again = false;
              await healOnce(projectId, conversationId);
            } while (entry.again);
          } finally {
            if (healing.get(key) === entry) healing.delete(key);
          }
        })();
        healing.set(key, entry);
        return entry.running;
      },

      dismissError: (projectId, conversationId) =>
        patch(chatKey(projectId, conversationId), (c) => ({ ...c, lastError: null })),

      reset: () => {
        generation += 1;
        opening.clear();
        healing.clear();
        subscribedOnSocket.clear();
        set({ chats: {}, activeRuns: {}, current: {} });
      },
    };
  });
}

export type ChatStore = ReturnType<typeof createChatStore>;

export const useChatStore: ChatStore = createChatStore({ ws: wsClient, fetchPage: fetchMessagesPage });
