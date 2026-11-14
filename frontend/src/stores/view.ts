import { createContext, useContext } from 'react';
import { createStore, type StoreApi, useStore } from 'zustand';
import { type WsClient, wsClient } from '@/api/ws';
import type { HighlightKind, KcHighlightBox } from '@/lib/kicanvas';
import { canonicalLayers, DEFAULT_LAYERS, type DocKind, type PcbLayer, sameViewFile, type ViewMode } from '@/lib/viewUrls';
import { readLiveView, saveLiveView } from './viewerPrefs';

type PerDoc<T> = Record<DocKind, T>;

/** View files per doc as frames and the open response carry them (root first). */
export type ViewFileLists = Partial<PerDoc<readonly string[]>>;

/** "Find REF" request; `seq` makes repeated searches for the same ref distinct. */
export interface FindRequest {
  ref: string;
  seq: number;
}

/**
 * A highlight item on a sheet instance. `sheetPath` undefined = the request's `sheetPath`;
 * null = whichever page is shown (refs are looked up there).
 */
export interface HighlightRef {
  ref: string;
  /** Default 'focus'. */
  kind?: HighlightKind;
  sheetPath?: string | null;
}
export interface HighlightBox extends KcHighlightBox {
  sheetPath?: string | null;
}

/**
 * Items to outline in the schematic viewer (E-C13). `refs` (references or UUIDs; plain strings
 * are 'focus' items) and `boxes` (mm) may span several sheet instances: the pane opens
 * `sheetPath` first (null: the page of the first ref) and applies, on every page it shows, the
 * items of that page. `seq` makes repeated requests distinct.
 */
export interface HighlightRequest {
  refs: Array<string | HighlightRef>;
  boxes: HighlightBox[];
  sheetPath: string | null;
  seq: number;
}

export interface ViewState {
  projectId: string | null;
  /** Last accepted `view-updated.version` per doc (server counter, index X2); null = none yet. */
  versions: PerDoc<number | null>;
  /**
   * "Data changed" counter per doc, bumped for every applied non-live `view-updated` (live frames
   * would re-render the agent view and refetch the Changes diff mid-turn; the turn-end frame always
   * follows them); `?v=` and reload key of the agent view, and the "schematic changed" signal of
   * the Changes panel. Unlike `versions` it never repeats, so a server counter that restarted (new
   * session) still reloads.
   */
  revs: PerDoc<number | null>;
  /**
   * Reload counter of the KiCad pane (`?v=` of its files), live frames included. Boards bump for
   * every applied frame; the schematic only when the sheet shown (or every sheet) changed, or a
   * stale sheet is opened.
   */
  kicadRevs: PerDoc<number | null>;
  /** "Live view" (per-browser pref): apply mid-turn (`live`) frames; off defers them to the turn end. */
  live: boolean;
  /** Filename (KiCanvas `KcPage.filename`) of the schematic page the KiCad pane shows. */
  activeSheet: string | null;
  /** Schematic view files changed since the pane loaded them (shown sheet unaffected): no reload yet. */
  staleSheets: string[];
  /**
   * The view files the KiCad pane loads per doc (root first): seeded from the open response, then
   * replaced by the `viewFiles` of applied frames whose set of files differs (sheets added or
   * removed by the agent); null = not known yet.
   */
  files: PerDoc<readonly string[] | null>;
  /**
   * Schematic sheets that appeared in `files` since the project was opened and were not opened yet:
   * marked like stale sheets, but loaded fresh already (opening one does not reload).
   */
  addedSheets: string[];
  /**
   * Bumped whenever a sheet is marked stale (also again); each stale entry remembers the value of
   * its last mark, so `clearStale` keeps sheets that changed again during a load.
   */
  staleSeq: number;
  doc: DocKind;
  mode: ViewMode;
  layers: PcbLayer[];
  fullscreen: boolean;
  /** Bumped on every project open (revs restart at null then). */
  openSeq: number;
  highlight: HighlightRequest | null;
  find: FindRequest | null;
  /**
   * Called on every project open: resets versions, sheet state, fullscreen, highlight and find;
   * `files` (the open response's `viewFiles`) seeds `files`.
   */
  setProject: (id: string, files?: ViewFileLists | null) => void;
  /** Persists the pref; turning it on applies the live changes deferred meanwhile. */
  setLive: (on: boolean) => void;
  /** Reported by the KiCad schematic pane; opening a stale sheet reloads fresh data. */
  setActiveSheet: (file: string | null) => void;
  /**
   * Reported by the KiCad schematic pane after a full load (it fetched every file fresh): `seq` is
   * the `staleSeq` when that load started; entries marked (again) since stay stale.
   */
  clearStale: (seq: number) => void;
  setDoc: (d: DocKind) => void;
  setMode: (m: ViewMode) => void;
  toggleLayer: (l: PcbLayer) => void;
  setFullscreen: (on: boolean) => void;
  /** Shows the KiCad schematic view with these items highlighted; returns the request's seq. */
  setHighlight: (h: Omit<HighlightRequest, 'seq'>) => number;
  clearHighlight: () => void;
  /** Find `ref` in the current document (switches to the KiCad view); null cancels. */
  setFind: (ref: string | null) => void;
  dispose: () => void;
}

const NONE: PerDoc<null> = { sch: null, pcb: null };

/** Changed view files of a frame (or a deferred batch); 'all' = unknown, assume everything. */
type Changes = Set<string> | 'all';

const union = (a: Changes | null, b: Changes): Changes =>
  a === null ? b : a === 'all' || b === 'all' ? 'all' : new Set([...a, ...b]);

const bump = (n: number | null) => (n ?? 0) + 1;

/**
 * Whether two view file lists name the same files with the same root (first entry). The order of
 * the rest may differ: the open response lists sheets in hierarchy order, frames sorted.
 */
const sameFileSet = (a: readonly string[], b: readonly string[]) => {
  if (a[0] !== b[0]) return false;
  const set = new Set(a);
  return set.size === new Set(b).size && b.every((f) => set.has(f));
};

/** The state a frame (or `setLive`) changes; `apply` mutates a copy of it. */
type Patch = Pick<ViewState, 'revs' | 'kicadRevs' | 'staleSheets' | 'staleSeq' | 'activeSheet' | 'files' | 'addedSheets'>;

export function createViewStore(ws: Pick<WsClient, 'onFrame' | 'onConnection'>): StoreApi<ViewState> {
  // Docs whose next view-updated is accepted even with a lower version: after a reconnect the
  // server may run a new session whose counter restarted at 0 (X2).
  let acceptAny: PerDoc<boolean> = { sch: false, pcb: false };
  // Changes of live frames received while "Live view" is off, applied with the next applied frame
  // (the turn-end publish, which may itself carry `changedFiles: []`) or when it is turned on.
  let deferred: PerDoc<Changes | null> = { ...NONE };
  // The latest `viewFiles` list of those deferred frames (full snapshots: the last one wins).
  let deferredFiles: PerDoc<readonly string[] | null> = { ...NONE };
  let seq = 0;
  // Per stale sheet: the `staleSeq` of its last mark.
  const staleGen = new Map<string, number>();

  /**
   * The state patch applying `changes` to doc `d` on top of `s` (mutated: callers pass copies).
   * `live` changes (incl. deferred ones applied by `setLive`) only reach the KiCad pane, not `revs`.
   * `list` is the doc's current view file list, if the frame (or a deferred one) carried it.
   */
  const apply = (s: Patch, d: DocKind, changes: Changes, live: boolean, list: readonly string[] | null) => {
    if (!live) s.revs[d] = bump(s.revs[d]);
    const old = s.files[d];
    const listChanged = list !== null && old !== null && !sameFileSet(old, list);
    if (list !== null && (old === null || listChanged)) s.files[d] = list;
    if (d === 'pcb') {
      s.kicadRevs.pcb = bump(s.kicadRevs.pcb);
      return;
    }
    if (listChanged) {
      // Sheets added or removed: reload every file fresh (unchanged URLs could come from cache).
      const added = list.filter((f) => f !== list[0] && !old.includes(f));
      const kept = s.addedSheets.filter((f) => list.includes(f));
      s.addedSheets = [...kept, ...added.filter((f) => !kept.includes(f))];
      s.kicadRevs.sch = bump(s.kicadRevs.sch);
      s.staleSheets = [];
      staleGen.clear();
      return;
    }
    // Only schematic files concern the sheets (a frame for both docs lists the board file too).
    const files = changes === 'all' ? 'all' : [...changes].filter((f) => f.endsWith('.kicad_sch'));
    if (files !== 'all' && files.length === 0) return;
    const active = s.activeSheet;
    if (files === 'all' || active === null || files.some((f) => sameViewFile(f, active))) {
      s.kicadRevs.sch = bump(s.kicadRevs.sch);
      s.staleSheets = []; // a reload loads every file fresh
      staleGen.clear();
    } else {
      s.staleSeq += 1;
      for (const f of files) staleGen.set(f, s.staleSeq);
      s.staleSheets = [...s.staleSheets, ...files.filter((f) => !s.staleSheets.includes(f))];
    }
  };
  const snapshot = (): Patch => {
    const s = store.getState();
    return {
      revs: { ...s.revs },
      kicadRevs: { ...s.kicadRevs },
      staleSheets: s.staleSheets,
      staleSeq: s.staleSeq,
      activeSheet: s.activeSheet,
      files: { ...s.files },
      addedSheets: s.addedSheets,
    };
  };

  const store = createStore<ViewState>()((set, get) => ({
    projectId: null,
    versions: { ...NONE },
    revs: { ...NONE },
    kicadRevs: { ...NONE },
    live: readLiveView(),
    activeSheet: null,
    staleSheets: [],
    staleSeq: 0,
    files: { ...NONE },
    addedSheets: [],
    doc: 'sch',
    mode: 'kicad',
    layers: [...DEFAULT_LAYERS],
    fullscreen: false,
    highlight: null,
    find: null,
    openSeq: 0,
    setProject: (id, files) => {
      acceptAny = { sch: false, pcb: false };
      deferred = { ...NONE };
      deferredFiles = { ...NONE };
      staleGen.clear();
      set({
        openSeq: get().openSeq + 1,
        projectId: id,
        versions: { ...NONE },
        revs: { ...NONE },
        kicadRevs: { ...NONE },
        activeSheet: null,
        staleSheets: [],
        files: { sch: files?.sch ?? null, pcb: files?.pcb ?? null },
        addedSheets: [],
        fullscreen: false,
        highlight: null,
        find: null,
      });
    },
    setLive: (live) => {
      saveLiveView(live);
      const next = { ...snapshot(), live };
      if (live) {
        for (const d of ['sch', 'pcb'] as const) {
          const changes = deferred[d];
          const list = deferredFiles[d];
          deferred[d] = null;
          deferredFiles[d] = null;
          if (changes !== null) apply(next, d, changes, true, list);
        }
      }
      set(next);
    },
    setActiveSheet: (activeSheet) => {
      const s = get();
      if (activeSheet === s.activeSheet) return;
      // an added sheet was loaded fresh with the list: opening it only drops its mark
      const left = activeSheet === null ? s.addedSheets : s.addedSheets.filter((f) => !sameViewFile(f, activeSheet));
      const addedSheets = left.length === s.addedSheets.length ? s.addedSheets : left;
      if (activeSheet !== null && s.staleSheets.some((f) => sameViewFile(f, activeSheet))) {
        // the pane reloads (all files fresh) and restores this sheet
        staleGen.clear();
        set({ activeSheet, addedSheets, staleSheets: [], kicadRevs: { ...s.kicadRevs, sch: bump(s.kicadRevs.sch) } });
      } else {
        set({ activeSheet, addedSheets });
      }
    },
    clearStale: (loadSeq) => {
      const s = get();
      const left = s.staleSheets.filter((f) => (staleGen.get(f) ?? 0) > loadSeq);
      if (left.length === s.staleSheets.length) return;
      for (const f of s.staleSheets) if (!left.includes(f)) staleGen.delete(f);
      set({ staleSheets: left });
    },
    setDoc: (doc) => set({ doc }),
    setMode: (mode) => set({ mode }),
    toggleLayer: (l) => {
      const cur = get().layers;
      set({ layers: canonicalLayers(cur.includes(l) ? cur.filter((x) => x !== l) : [...cur, l]) });
    },
    setFullscreen: (fullscreen) => set({ fullscreen }),
    setHighlight: (h) => {
      set({ highlight: { ...h, seq: ++seq }, doc: 'sch', mode: 'kicad' });
      return seq;
    },
    clearHighlight: () => set({ highlight: null }),
    setFind: (ref) => set(ref === null ? { find: null } : { find: { ref, seq: ++seq }, mode: 'kicad' }),
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
    const next = snapshot();
    let changed = false;
    for (const d of f.docs) {
      const cur = versions[d];
      if (!acceptAny[d] && cur !== null && f.version <= cur) continue; // replayed / stale frame
      // After a reconnect frames may have been missed: their changed files are unknown.
      const changes: Changes = acceptAny[d] || !f.changedFiles ? 'all' : new Set(f.changedFiles);
      acceptAny[d] = false;
      versions[d] = f.version;
      changed = true;
      const list = f.viewFiles?.[d] ?? null;
      if (f.live && !s.live) {
        deferred[d] = union(deferred[d], changes); // versions advance, so replays stay rejected
        deferredFiles[d] = list ?? deferredFiles[d];
        continue;
      }
      const all = union(deferred[d], changes);
      const files = list ?? deferredFiles[d];
      deferred[d] = null;
      deferredFiles[d] = null;
      apply(next, d, all, Boolean(f.live), files);
    }
    if (changed) store.setState({ versions, ...next });
  });
  const offConn = ws.onConnection((state) => {
    // A reconnect after a backend restart reports 'open' (not 'resumed'); either way the
    // server may run a new KiCad session whose counter restarted at 0.
    if (state === 'open' || state === 'resumed') acceptAny = { sch: true, pcb: true };
  });
  return store;
}

export const viewStore = createViewStore(wsClient);
export const ViewStoreContext = createContext<StoreApi<ViewState> | null>(null);

export function useView<T>(selector: (s: ViewState) => T): T {
  return useStore(useContext(ViewStoreContext) ?? viewStore, selector);
}
