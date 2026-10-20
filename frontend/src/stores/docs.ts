import { createContext, useContext } from 'react';
import { createStore, type StoreApi, useStore } from 'zustand';
import { type DocConflict, type DocIndexDTO, type DocRead, getDoc, getDocIndex } from '@/api/docs';
import { type WsClient, wsClient } from '@/api/ws';
import { errorText } from '@/components/history/restore';

export interface DocsDeps {
  getIndex: (projectId: string) => Promise<DocIndexDTO>;
  getDoc: (projectId: string, path: string) => Promise<DocRead>;
}

/** The newest `doc-updated` frame of a path. */
export interface DocLiveUpdate {
  version: string;
  author: 'agent' | 'user' | 'external';
  conversationId?: string;
}

/** An unsaved editor state: it survives the editor's unmount (sheet closed, panel switched, …). */
export interface DocDraft {
  text: string;
  /** What `text` is written on top of: a save sends `version` as `baseVersion`. */
  base: { version: string | null; body: string; approved: boolean };
  conflict: DocConflict | null;
}

export const draftKey = (projectId: string, path: string) => `${projectId}/${path}`;

export interface DocsState {
  projectId: string | null;
  index: DocIndexDTO | null;
  indexError: string | null;
  /** The doc shown in the panel (view, editor, history), or null on the index. */
  openPath: string | null;
  doc: DocRead | null;
  docError: string | null;
  /** Per path, the last `doc-updated` seen: an editor compares it with its base version. */
  live: Record<string, DocLiveUpdate>;
  /** Unsaved drafts by draftKey(projectId, path); kept across project switches until saved or discarded. */
  drafts: Record<string, DocDraft>;
  setDraft: (projectId: string, path: string, draft: DocDraft | null) => void;
  load: (projectId: string) => Promise<void>;
  refreshIndex: () => Promise<void>;
  /** Shows `path` (null: back to the index) and loads it. */
  open: (path: string | null) => Promise<void>;
  reloadDoc: () => Promise<void>;
  /** The panel closed: stop following frames until the next load(). */
  detach: () => void;
  dispose: () => void;
}

const DEFAULT_DEPS: DocsDeps = { getIndex: getDocIndex, getDoc };

/** Fixed docs first (in server order), then the extra docs. */
function ordered(index: DocIndexDTO): DocIndexDTO {
  return { ...index, docs: [...index.docs.filter((d) => d.fixed), ...index.docs.filter((d) => !d.fixed)] };
}

/**
 * Coalesces refreshes: while one runs, further calls only mark it dirty, and one more run
 * follows. A burst of `doc-updated` frames costs at most two requests.
 */
function coalesced(run: () => Promise<void>): () => Promise<void> {
  let running: Promise<void> | null = null;
  let dirty = false;
  const loop = async () => {
    do {
      dirty = false;
      await run();
    } while (dirty);
    running = null;
  };
  return () => {
    if (running) {
      dirty = true;
      return running;
    }
    running = loop();
    return running;
  };
}

/**
 * Design docs of the open project (sessions C4, spec §12.2). `doc-updated` frames refresh the
 * index and, for its path, the open doc; editors keep their own draft and read `live`.
 */
export function createDocsStore(
  ws: Pick<WsClient, 'onFrame' | 'onConnection'>,
  deps: DocsDeps = DEFAULT_DEPS,
): StoreApi<DocsState> {
  // Bumped per project switch: responses of an older project are dropped.
  let generation = 0;
  // Bumped per open(): a slower response of a previously opened doc is dropped.
  let docToken = 0;
  // Per request: only the newest started request may apply its response.
  let indexSeq = 0;
  let docSeq = 0;
  // Frames and reconnects only refetch while a Docs panel is attached.
  let attached = false;

  const fetchIndex = async () => {
    const { projectId } = store.getState();
    if (!projectId) return;
    const gen = generation;
    const seq = ++indexSeq;
    const current = () => gen === generation && seq === indexSeq;
    try {
      const index = ordered(await deps.getIndex(projectId));
      if (current()) store.setState({ index, indexError: null });
    } catch (e) {
      if (current()) store.setState({ indexError: `Could not load the docs: ${errorText(e)}` });
    }
  };

  const fetchDoc = async () => {
    const { projectId, openPath } = store.getState();
    if (!projectId || !openPath) return;
    const gen = generation;
    const token = docToken;
    const seq = ++docSeq;
    const current = () => gen === generation && token === docToken && seq === docSeq;
    try {
      const doc = await deps.getDoc(projectId, openPath);
      if (current()) store.setState({ doc, docError: null });
    } catch (e) {
      if (current()) store.setState({ docError: `Could not load the doc: ${errorText(e)}` });
    }
  };

  const refreshIndex = coalesced(fetchIndex);
  const refreshDoc = coalesced(fetchDoc);

  const store = createStore<DocsState>()((set, get) => ({
    projectId: null,
    index: null,
    indexError: null,
    openPath: null,
    doc: null,
    docError: null,
    live: {},
    drafts: {},
    setDraft: (projectId, path, draft) => {
      const key = draftKey(projectId, path);
      const { [key]: _old, ...rest } = get().drafts;
      set({ drafts: draft ? { ...rest, [key]: draft } : rest });
    },
    load: async (projectId) => {
      attached = true;
      if (get().projectId !== projectId) {
        generation++;
        docToken++;
        set({ projectId, index: null, indexError: null, openPath: null, doc: null, docError: null, live: {} });
        await fetchIndex(); // not coalesced: a request of the previous project may still run
        return;
      }
      await refreshIndex();
    },
    refreshIndex,
    open: async (path) => {
      docToken++;
      set({ openPath: path, doc: null, docError: null });
      if (path) await fetchDoc();
    },
    reloadDoc: refreshDoc,
    detach: () => {
      attached = false;
      docToken++;
      set({ openPath: null, doc: null, docError: null });
    },
    dispose: () => {
      offFrame();
      offConn();
      if (typeof window !== 'undefined') window.removeEventListener('beforeunload', onBeforeUnload);
    },
  }));

  const offFrame = ws.onFrame((f) => {
    if (f.type !== 'doc-updated') return;
    const s = store.getState();
    if (f.projectId !== s.projectId) return;
    const update: DocLiveUpdate = { version: f.version, author: f.author };
    if (f.conversationId) update.conversationId = f.conversationId;
    store.setState({ live: { ...s.live, [f.path]: update } });
    if (!attached) return;
    void refreshIndex();
    if (f.path === s.openPath) void refreshDoc();
  });
  const offConn = ws.onConnection((state) => {
    if (!attached || (state !== 'open' && state !== 'resumed')) return;
    // Frames may have been missed while disconnected.
    void refreshIndex();
    void refreshDoc();
  });
  // An unsaved draft asks before the tab is closed or reloaded.
  const onBeforeUnload = (e: BeforeUnloadEvent) => {
    if (Object.keys(store.getState().drafts).length > 0) e.preventDefault();
  };
  if (typeof window !== 'undefined') window.addEventListener('beforeunload', onBeforeUnload);
  return store;
}

export const docsStore = createDocsStore(wsClient);
export const DocsStoreContext = createContext<StoreApi<DocsState> | null>(null);

export function useDocsStore(): StoreApi<DocsState> {
  return useContext(DocsStoreContext) ?? docsStore;
}

export function useDocs<T>(selector: (s: DocsState) => T): T {
  return useStore(useDocsStore(), selector);
}
