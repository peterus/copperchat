import { z } from 'zod';
import { create } from 'zustand';
import { apiGet } from '@/api/http';
import { wsClient, type ConnectionState, type WsClient } from '@/api/ws';
import {
  AttachmentRef,
  ClientFrame,
  MessagesPage,
  type MessageDeltaFrame,
  type MessageDTO,
  type ServerFrame,
  type Usage,
} from '@/protocol/frames';

export const PAGE_SIZE = 50;
const TERMINAL_STATUSES: ReadonlySet<string> = new Set(['done', 'cancelled', 'error']);

export interface ProjectChat {
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

export function emptyChat(): ProjectChat {
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
  fetchPage: (projectId: string, opts: { limit: number; before?: number }) => Promise<MessagesPage>;
  newClientMessageId?: () => string;
}

export interface ChatState {
  chats: Readonly<Record<string, ProjectChat>>;
  open: (projectId: string) => Promise<void>;
  loadOlder: (projectId: string) => Promise<void>;
  /** False (and lastError set) when the frame would be refused by the protocol; nothing is queued then. */
  send: (projectId: string, text: string, attachments?: AttachmentRef[]) => boolean;
  cancelQueued: (projectId: string, messageId: number) => void;
  stop: (projectId: string) => void;
  heal: (projectId: string) => Promise<void>;
  dismissError: (projectId: string) => void;
  /** Drops every project's chat (logout / 4401); results of in-flight requests are discarded. */
  reset: () => void;
}

/** GET /api/projects/{id}/messages: the newest `limit` rows below `before`, ascending by id. */
export async function fetchMessagesPage(
  projectId: string,
  opts: { limit: number; before?: number },
): Promise<MessagesPage> {
  const q = new URLSearchParams({ limit: String(opts.limit) });
  if (opts.before !== undefined) q.set('before', String(opts.before));
  const raw = await apiGet<unknown>(`/projects/${encodeURIComponent(projectId)}/messages?${q.toString()}`);
  return MessagesPage.parse(raw);
}

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
function withStreamingSuspects(c: ProjectChat, page: MessageDTO[]): ProjectChat {
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
  const opening = new Map<string, Promise<void>>();
  const healing = new Map<string, { running: Promise<void>; again: boolean }>();
  // Bumped by reset(): an HTTP response that started in an older generation is ignored.
  let generation = 0;
  let optimisticSeq = 0;
  const newClientMessageId = deps.newClientMessageId ?? randomClientMessageId;

  return create<ChatState>()((set, get) => {
    function patch(projectId: string, fn: (c: ProjectChat) => ProjectChat): void {
      const current = get().chats[projectId];
      if (!current) return;
      const next = fn(current);
      if (next !== current) set({ chats: { ...get().chats, [projectId]: next } });
    }

    function withRows(c: ProjectChat, rows: MessageDTO[]): ProjectChat {
      return { ...c, messages: mergeRows(c.messages, rows), lastSeenId: Math.max(c.lastSeenId, maxServerId(rows)) };
    }

    /** Only with an open socket (returns false otherwise); the connection handler subscribes on open. */
    function subscribe(projectId: string): boolean {
      const c = get().chats[projectId];
      if (!c?.loaded || !deps.ws.isOpen()) return false;
      // Re-subscribing replaces the server-side subscription (R4); there is no unsubscribe.
      deps.ws.send({ type: 'subscribe', projectId, sinceMessageId: c.lastSeenId });
      return true;
    }

    /** Server dedupes on clientMessageId, so retransmitting is always safe. */
    function resendPending(projectId: string): void {
      const c = get().chats[projectId];
      if (!c) return;
      for (const m of c.messages) {
        if (m.id >= 0 || m.status === 'error' || m.clientMessageId === null) continue;
        try {
          deps.ws.send({ type: 'send', projectId, clientMessageId: m.clientMessageId, text: m.content, attachments: attachmentsOf(m) });
        } catch (e) {
          // An unsendable row must not break every reconnect: mark it failed instead.
          const error = `not sendable: ${errorText(e)}`;
          patch(projectId, (cur) => ({
            ...cur,
            messages: cur.messages.map((x) =>
              x.id === m.id ? { ...x, status: 'error' as const, metadata: { ...x.metadata, error } } : x,
            ),
          }));
        }
      }
    }

    function onDelta(projectId: string, f: MessageDeltaFrame): void {
      const c = get().chats[projectId];
      if (!c) return;
      const existing = c.messages.find((m) => m.id === f.messageId);
      if (!existing) {
        // Its `message` frame was lost (e.g. mid-reconnect): heal instead of dropping.
        patch(projectId, (cur) => ({
          ...cur,
          suspectIds: cur.suspectIds.includes(f.messageId) ? cur.suspectIds : [...cur.suspectIds, f.messageId],
        }));
        void get().heal(projectId);
        return;
      }
      const updated = applyDelta(existing, f);
      const settledSuspect =
        c.suspectIds.includes(f.messageId) && f.statusPatch !== undefined && TERMINAL_STATUSES.has(f.statusPatch);
      patch(projectId, (cur) => ({
        ...cur,
        messages: cur.messages.map((m) => (m.id === updated.id ? updated : m)),
        suspectIds: settledSuspect ? cur.suspectIds.filter((id) => id !== f.messageId) : cur.suspectIds,
      }));
      // A suspect may have lost deltas; once final, the DB row is authoritative.
      if (settledSuspect) void get().heal(projectId);
    }

    /** Frames may have been lost with the socket: a run-finished (stuck Stop) or deltas. */
    function markDisconnected(c: ProjectChat): ProjectChat {
      const streaming = c.messages.filter((m) => m.id > 0 && m.status === 'streaming').map((m) => m.id);
      const suspectIds = [...new Set([...c.suspectIds, ...streaming])];
      if (!c.runActive && c.currentRunId === null && suspectIds.length === c.suspectIds.length) return c;
      return { ...c, runActive: false, currentRunId: null, suspectIds };
    }

    function onConnection(state: ConnectionState): void {
      for (const [projectId, chat] of Object.entries(get().chats)) {
        if (!chat.loaded) {
          // An in-flight initial load subscribes itself with the right lastSeenId; a failed one is retried.
          if ((state === 'open' || state === 'resumed') && !opening.has(projectId)) void get().open(projectId);
          continue;
        }
        // The server re-sends run-started on subscribe while the run is still active, so
        // resetting here never hides a live run for longer than the replay.
        patch(projectId, markDisconnected);
        // closed / unauthorized / forbidden: nothing to talk to; 'open' follows on reconnect.
        if (state !== 'open' && state !== 'resumed') continue;
        subscribe(projectId);
        resendPending(projectId);
        void get().heal(projectId); // half-streamed rows: subscribe(since) never re-sends rows <= lastSeenId
      }
    }

    function onFrame(f: ServerFrame): void {
      if (f.type === 'error' && f.projectId === null) {
        for (const projectId of Object.keys(get().chats)) patch(projectId, (c) => ({ ...c, lastError: f.message }));
        return;
      }
      if (f.projectId === null || !get().chats[f.projectId]) return;
      const projectId = f.projectId;
      switch (f.type) {
        case 'message':
          patch(projectId, (c) => withRows(c, [f.message]));
          return;
        case 'message-delta':
          onDelta(projectId, f);
          return;
        case 'run-started':
          patch(projectId, (c) => ({ ...c, runActive: true, currentRunId: f.runId, rateLimitedUntil: null, lastError: null }));
          return;
        case 'run-finished':
          // A suspect's terminal delta may have been lost (it is sent once, never replayed): heal.
          if (get().chats[projectId]!.suspectIds.length > 0) void get().heal(projectId);
          patch(projectId, (c) => {
            if (c.currentRunId !== null && c.currentRunId !== f.runId) return c; // late finish of an older run
            return {
              ...c,
              runActive: false,
              currentRunId: null,
              lastError: f.reason === 'error' ? (c.lastError ?? 'The agent run ended with an error.') : c.lastError,
            };
          });
          return;
        case 'usage':
          patch(projectId, (c) => ({ ...c, usage: f.usage }));
          return;
        case 'rate-limited':
          patch(projectId, (c) => ({ ...c, rateLimitedUntil: f.resetsAt }));
          return;
        case 'error':
          // cancelQueued marked the row cancelled optimistically; the server row is authoritative.
          if (f.code === 'not_cancellable') void get().heal(projectId);
          patch(projectId, (c) => ({
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
          return;
        case 'view-updated': // consumed by the viewer (Phase 7); ping/pong never reach here (projectId null)
        case 'changes-updated': // consumed by the Changes panel (E9a)
          return;
      }
    }

    async function load(projectId: string): Promise<void> {
      if (!get().chats[projectId]) set({ chats: { ...get().chats, [projectId]: emptyChat() } });
      else patch(projectId, (c) => (c.loadFailed ? { ...c, loadFailed: false } : c));
      const gen = generation;
      let streaming = false;
      try {
        const page = await deps.fetchPage(projectId, { limit: PAGE_SIZE });
        if (gen !== generation) return;
        streaming = page.messages.some((r) => r.id > 0 && r.status === 'streaming');
        patch(projectId, (c) => {
          const next = withStreamingSuspects(withRows(c, page.messages), page.messages);
          return { ...next, loaded: true, hasMoreOlder: page.hasMore, oldestId: minServerId(next.messages), lastError: null };
        });
      } catch (e) {
        if (gen === generation) patch(projectId, (c) => ({ ...c, loadFailed: true, lastError: `Could not load messages: ${errorText(e)}` }));
        return;
      }
      // A suspect's terminal delta sent between the GET and the subscribe is lost (replay only sends
      // id > since): re-read the DB once subscribed. Without an open socket, 'open' heals anyway.
      if (subscribe(projectId) && streaming) await get().heal(projectId);
    }

    async function healOnce(projectId: string): Promise<void> {
      const gen = generation;
      try {
        const page = await deps.fetchPage(projectId, { limit: PAGE_SIZE });
        if (gen !== generation) return;
        patch(projectId, (c) => {
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

      open: (projectId) => {
        // Always (re)enable the socket: after a logout/4401 the client stays down until ensure().
        deps.ws.ensure();
        if (get().chats[projectId]?.loaded) return Promise.resolve();
        const inflight = opening.get(projectId);
        if (inflight) return inflight;
        const p = load(projectId).finally(() => {
          if (opening.get(projectId) === p) opening.delete(projectId);
        });
        opening.set(projectId, p);
        return p;
      },

      loadOlder: async (projectId) => {
        const c = get().chats[projectId];
        if (!c?.loaded || c.loadingOlder || !c.hasMoreOlder || c.oldestId === null) return;
        const before = c.oldestId;
        patch(projectId, (cur) => ({ ...cur, loadingOlder: true }));
        const gen = generation;
        try {
          const page = await deps.fetchPage(projectId, { limit: PAGE_SIZE, before });
          if (gen !== generation) return;
          patch(projectId, (cur) => {
            const messages = mergeRows(cur.messages, page.messages);
            return { ...cur, messages, oldestId: minServerId(messages), hasMoreOlder: page.hasMore, loadingOlder: false };
          });
        } catch (e) {
          if (gen !== generation) return;
          patch(projectId, (cur) => ({ ...cur, loadingOlder: false, lastError: `Could not load older messages: ${errorText(e)}` }));
        }
      },

      send: (projectId, text, attachments = []) => {
        if (!get().chats[projectId] || (text.trim() === '' && attachments.length === 0)) return false;
        const clientMessageId = newClientMessageId();
        const frame: ClientFrame = { type: 'send', projectId, clientMessageId, text, attachments };
        // Validate before the optimistic insert: a row the protocol refuses would be stuck forever.
        const issue = frameIssue(frame);
        if (issue !== null) {
          patch(projectId, (c) => ({ ...c, lastError: `Message not sent: ${issue}` }));
          return false;
        }
        optimisticSeq += 1;
        const optimistic: MessageDTO = {
          id: -optimisticSeq,
          projectId,
          clientMessageId,
          runId: null,
          role: 'user',
          kind: 'text',
          content: text,
          status: 'queued',
          metadata: { pending: true, attachments },
          createdAt: new Date().toISOString(),
        };
        patch(projectId, (c) => ({ ...c, messages: [...c.messages, optimistic].sort(compareMessages) }));
        // Not open: resendPending() sends it on 'open' (sending now would queue a duplicate).
        if (deps.ws.isOpen()) deps.ws.send(frame);
        return true;
      },

      cancelQueued: (projectId, messageId) => {
        if (messageId <= 0) return;
        deps.ws.send({ type: 'cancel-queued', projectId, messageId });
        patch(projectId, (c) => ({
          ...c,
          messages: c.messages.map((m) => (m.id === messageId ? { ...m, status: 'cancelled' as const } : m)),
        }));
      },

      stop: (projectId) => {
        const c = get().chats[projectId];
        if (!c) return;
        deps.ws.send({ type: 'interrupt', projectId, runId: c.currentRunId });
      },

      heal: (projectId) => {
        const state = healing.get(projectId);
        if (state) {
          state.again = true; // a newer request arrived mid-fetch: fetch once more afterwards
          return state.running;
        }
        const entry = { running: Promise.resolve(), again: false };
        entry.running = (async () => {
          try {
            do {
              entry.again = false;
              await healOnce(projectId);
            } while (entry.again);
          } finally {
            if (healing.get(projectId) === entry) healing.delete(projectId);
          }
        })();
        healing.set(projectId, entry);
        return entry.running;
      },

      dismissError: (projectId) => patch(projectId, (c) => ({ ...c, lastError: null })),

      reset: () => {
        generation += 1;
        opening.clear();
        healing.clear();
        set({ chats: {} });
      },
    };
  });
}

export type ChatStore = ReturnType<typeof createChatStore>;

export const useChatStore: ChatStore = createChatStore({ ws: wsClient, fetchPage: fetchMessagesPage });
