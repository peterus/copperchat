/**
 * The sessions (conversations) of each opened project (S-C8, spec §12.1): the lists, which
 * `conversation-updated` frames keep current, the project's running session (`activeRun`) and
 * the unread marks of the session list.
 */
import { createContext, useContext } from 'react';
import { create } from 'zustand';
import {
  closeConversation,
  createConversation,
  listConversations,
  patchConversation,
  reopenConversation,
} from '@/api/conversations';
import { wsClient, type ConnectionState, type WsClient } from '@/api/ws';
import type { ConversationDTO, ConversationType, ServerFrame } from '@/protocol/frames';

export interface ConversationsApi {
  list: (projectId: string, opts?: { archived?: boolean }) => Promise<ConversationDTO[]>;
  create: (projectId: string, body: { type: ConversationType; title?: string }) => Promise<ConversationDTO>;
  patch: (projectId: string, conversationId: string, body: { title?: string; archived?: boolean }) => Promise<ConversationDTO>;
  close: (projectId: string, conversationId: string) => Promise<ConversationDTO>;
  reopen: (projectId: string, conversationId: string) => Promise<ConversationDTO>;
}

export interface ConversationsDeps {
  ws: WsClient;
  api: ConversationsApi;
}

/** The project's running run; one burst runs at a time per project (FIFO across sessions). */
export interface ActiveRun {
  conversationId: string;
  runId: string;
}

export type ListStatus = 'loading' | 'loaded' | 'error';

export interface ConversationsState {
  /** Per project: every session known (archived ones once listed), newest activity first. */
  byProject: Readonly<Record<string, ConversationDTO[]>>;
  status: Readonly<Record<string, ListStatus>>;
  errors: Readonly<Record<string, string | null>>;
  /** Per project: the running session, or null. */
  activeRun: Readonly<Record<string, ActiveRun | null>>;
  /** Per project: sessions with messages that arrived while another one was shown. */
  unread: Readonly<Record<string, string[]>>;
  /** Per project: the session shown (its messages are never unread). */
  viewing: Readonly<Record<string, string | null>>;
  /** Lists the project's sessions (one request at a time per project). */
  load: (projectId: string) => Promise<void>;
  /** Adds the archived sessions (once per project). */
  loadArchived: (projectId: string) => Promise<void>;
  view: (projectId: string, conversationId: string | null) => void;
  create: (projectId: string, body: { type: ConversationType; title?: string }) => Promise<ConversationDTO>;
  rename: (projectId: string, conversationId: string, title: string) => Promise<void>;
  setArchived: (projectId: string, conversationId: string, archived: boolean) => Promise<void>;
  /** POST …/close (202, `closing`): the handover turn is enqueued. */
  close: (projectId: string, conversationId: string) => Promise<void>;
  reopen: (projectId: string, conversationId: string) => Promise<void>;
  /** Stops the project's running session (whichever is shown). */
  interrupt: (projectId: string) => void;
  /** Drops everything (logout / 4401); results of in-flight requests are discarded. */
  reset: () => void;
}

const activity = (c: ConversationDTO) => c.lastMessageAt ?? c.createdAt;

/** Newest activity first (last message, else creation), then the higher seq: as the server lists. */
export function byActivity(a: ConversationDTO, b: ConversationDTO): number {
  const x = activity(a);
  const y = activity(b);
  return x === y ? b.seq - a.seq : x < y ? 1 : -1;
}

function upsertInto(list: ConversationDTO[], items: ConversationDTO[]): ConversationDTO[] {
  const byId = new Map(list.map((c) => [c.id, c] as const));
  for (const c of items) byId.set(c.id, c);
  return [...byId.values()].sort(byActivity);
}

const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function createConversationsStore(deps: ConversationsDeps) {
  const loading = new Map<string, Promise<void>>();
  const archivedLoaded = new Set<string>();
  let generation = 0;

  return create<ConversationsState>()((set, get) => {
    function upsert(projectId: string, items: ConversationDTO[]): void {
      set({ byProject: { ...get().byProject, [projectId]: upsertInto(get().byProject[projectId] ?? [], items) } });
    }

    function setActiveRun(projectId: string, run: ActiveRun | null): void {
      set({ activeRun: { ...get().activeRun, [projectId]: run } });
    }

    function onFrame(f: ServerFrame): void {
      if (f.projectId === null || get().byProject[f.projectId] === undefined) return;
      const projectId = f.projectId;
      switch (f.type) {
        case 'conversation-updated':
          upsert(projectId, [f.conversation]);
          return;
        case 'run-started':
          setActiveRun(projectId, { conversationId: f.conversationId, runId: f.runId });
          return;
        case 'run-finished':
          if (get().activeRun[projectId]?.runId === f.runId) setActiveRun(projectId, null);
          return;
        case 'message': {
          const cid = f.message.conversationId;
          const unread = get().unread[projectId] ?? [];
          if (cid === get().viewing[projectId] || unread.includes(cid)) return;
          set({ unread: { ...get().unread, [projectId]: [...unread, cid] } });
          return;
        }
        default:
          return;
      }
    }

    function onConnection(state: ConnectionState): void {
      // The server re-sends run-started on subscribe while the run is still active.
      const activeRun = Object.fromEntries(Object.keys(get().activeRun).map((pid) => [pid, null]));
      set({ activeRun });
      if (state !== 'open' && state !== 'resumed') return;
      // Frames may have been lost with the socket: the lists are read again.
      for (const projectId of Object.keys(get().byProject)) {
        void get().load(projectId);
        if (archivedLoaded.has(projectId)) {
          archivedLoaded.delete(projectId);
          void get().loadArchived(projectId);
        }
      }
    }

    async function fetchList(projectId: string, archived: boolean): Promise<void> {
      const gen = generation;
      if (!archived) set({ status: { ...get().status, [projectId]: 'loading' } });
      try {
        const list = await deps.api.list(projectId, archived ? { archived: true } : {});
        if (gen !== generation) return;
        upsert(projectId, list);
        if (!archived) {
          set({ status: { ...get().status, [projectId]: 'loaded' }, errors: { ...get().errors, [projectId]: null } });
        }
      } catch (e) {
        if (gen !== generation) return;
        if (archived) archivedLoaded.delete(projectId);
        set({
          status: archived ? get().status : { ...get().status, [projectId]: 'error' },
          errors: { ...get().errors, [projectId]: errorText(e) },
        });
      }
    }

    deps.ws.onFrame(onFrame);
    deps.ws.onConnection(onConnection);

    return {
      byProject: {},
      status: {},
      errors: {},
      activeRun: {},
      unread: {},
      viewing: {},

      load: (projectId) => {
        const inflight = loading.get(projectId);
        if (inflight) return inflight;
        const p = fetchList(projectId, false).finally(() => {
          if (loading.get(projectId) === p) loading.delete(projectId);
        });
        loading.set(projectId, p);
        return p;
      },

      loadArchived: async (projectId) => {
        if (archivedLoaded.has(projectId)) return;
        archivedLoaded.add(projectId);
        await fetchList(projectId, true);
      },

      view: (projectId, conversationId) => {
        const unread = get().unread[projectId] ?? [];
        set({
          viewing: { ...get().viewing, [projectId]: conversationId },
          unread: { ...get().unread, [projectId]: unread.filter((id) => id !== conversationId) },
        });
      },

      create: async (projectId, body) => {
        const c = await deps.api.create(projectId, body);
        upsert(projectId, [c]);
        return c;
      },

      rename: async (projectId, conversationId, title) => {
        upsert(projectId, [await deps.api.patch(projectId, conversationId, { title })]);
      },

      setArchived: async (projectId, conversationId, archived) => {
        upsert(projectId, [await deps.api.patch(projectId, conversationId, { archived })]);
      },

      close: async (projectId, conversationId) => {
        upsert(projectId, [await deps.api.close(projectId, conversationId)]);
      },

      reopen: async (projectId, conversationId) => {
        upsert(projectId, [await deps.api.reopen(projectId, conversationId)]);
      },

      interrupt: (projectId) => {
        const run = get().activeRun[projectId];
        if (!run) return;
        deps.ws.send({ type: 'interrupt', projectId, conversationId: run.conversationId, runId: run.runId });
      },

      reset: () => {
        generation += 1;
        loading.clear();
        archivedLoaded.clear();
        set({ byProject: {}, status: {}, errors: {}, activeRun: {}, unread: {}, viewing: {} });
      },
    };
  });
}

export type ConversationsStore = ReturnType<typeof createConversationsStore>;

export const useConversationsStore: ConversationsStore = createConversationsStore({
  ws: wsClient,
  api: {
    list: listConversations,
    create: createConversation,
    patch: patchConversation,
    close: closeConversation,
    reopen: reopenConversation,
  },
});

/** The app uses the singleton store; tests inject one backed by FakeWsClient and a fake API. */
export const ConversationsStoreContext = createContext<ConversationsStore>(useConversationsStore);

export function useConversations<T>(selector: (s: ConversationsState) => T): T {
  return useContext(ConversationsStoreContext)(selector);
}

/** The store itself (for actions: `useConversationsApi().getState().close(…)`). */
export function useConversationsApi(): ConversationsStore {
  return useContext(ConversationsStoreContext);
}

/** One session of the project, or undefined while unknown. */
export function useConversation(projectId: string, conversationId: string | null): ConversationDTO | undefined {
  return useConversations((s) =>
    conversationId === null ? undefined : s.byProject[projectId]?.find((c) => c.id === conversationId),
  );
}
