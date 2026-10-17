import { createContext, useContext } from 'react';
import { createStore, type StoreApi, useStore } from 'zustand';
import { type WsClient, wsClient } from '@/api/ws';
import { canonicalLayers, DEFAULT_LAYERS, type DocKind, type PcbLayer, type ViewMode } from '@/lib/viewUrls';

type PerDoc<T> = Record<DocKind, T>;

export interface ViewState {
  projectId: string | null;
  /** Last accepted `view-updated.version` per doc (server counter, index X2); null = none yet. */
  versions: PerDoc<number | null>;
  /**
   * Local reload counter per doc, bumped for every accepted `view-updated`; used as `?v=` and as
   * the reload key of the panes. Unlike `versions` it never repeats, so a server counter that
   * restarted (new session) still reloads.
   */
  revs: PerDoc<number | null>;
  doc: DocKind;
  mode: ViewMode;
  layers: PcbLayer[];
  fullscreen: boolean;
  /** Called on every project open: resets versions and fullscreen. */
  setProject: (id: string) => void;
  setDoc: (d: DocKind) => void;
  setMode: (m: ViewMode) => void;
  toggleLayer: (l: PcbLayer) => void;
  setFullscreen: (on: boolean) => void;
  dispose: () => void;
}

const NONE: PerDoc<null> = { sch: null, pcb: null };

export function createViewStore(ws: Pick<WsClient, 'onFrame' | 'onConnection'>): StoreApi<ViewState> {
  // Docs whose next view-updated is accepted even with a lower version: after a reconnect the
  // server may run a new session whose counter restarted at 0 (X2).
  let acceptAny: PerDoc<boolean> = { sch: false, pcb: false };
  const store = createStore<ViewState>()((set, get) => ({
    projectId: null,
    versions: { ...NONE },
    revs: { ...NONE },
    doc: 'sch',
    mode: 'kicad',
    layers: [...DEFAULT_LAYERS],
    fullscreen: false,
    setProject: (id) => {
      acceptAny = { sch: false, pcb: false };
      set({ projectId: id, versions: { ...NONE }, revs: { ...NONE }, fullscreen: false });
    },
    setDoc: (doc) => set({ doc }),
    setMode: (mode) => set({ mode }),
    toggleLayer: (l) => {
      const cur = get().layers;
      set({ layers: canonicalLayers(cur.includes(l) ? cur.filter((x) => x !== l) : [...cur, l]) });
    },
    setFullscreen: (fullscreen) => set({ fullscreen }),
    dispose: () => {
      offFrame();
      offConn();
    },
  }));
  const offFrame = ws.onFrame((f) => {
    if (f.type !== 'view-updated') return;
    const s = store.getState();
    if (f.projectId !== s.projectId) return;
    const versions = { ...s.versions };
    const revs = { ...s.revs };
    let changed = false;
    for (const d of f.docs) {
      const cur = versions[d];
      if (!acceptAny[d] && cur !== null && f.version <= cur) continue; // replayed / stale frame
      acceptAny[d] = false;
      versions[d] = f.version;
      revs[d] = (revs[d] ?? 0) + 1;
      changed = true;
    }
    if (changed) store.setState({ versions, revs });
  });
  const offConn = ws.onConnection((state) => {
    if (state === 'resumed') acceptAny = { sch: true, pcb: true };
  });
  return store;
}

export const viewStore = createViewStore(wsClient);
export const ViewStoreContext = createContext<StoreApi<ViewState> | null>(null);

export function useView<T>(selector: (s: ViewState) => T): T {
  return useStore(useContext(ViewStoreContext) ?? viewStore, selector);
}
