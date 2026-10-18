import { createContext, useContext } from 'react';
import { createStore, type StoreApi, useStore } from 'zustand';
import { type ChangeEntry, type ChangeRun, fetchChangeRuns, fetchChanges } from '@/api/changes';
import { type WsClient, wsClient } from '@/api/ws';
import { errorText } from '@/components/history/restore';

/** Frames arrive per tool call during a turn: one refetch per burst. */
export const CHANGES_DEBOUNCE_MS = 300;

/** Key of `entries`: a run, or one plan step of a run. */
export const entriesKey = (runId: string, stepId?: string | null) => `${runId}|${stepId ?? ''}`;

export interface ChangesDeps {
  fetchRuns: (projectId: string) => Promise<ChangeRun[]>;
  fetchEntries: (projectId: string, runId: string, stepId: string | null) => Promise<ChangeEntry[]>;
}

export interface ChangesState {
  projectId: string | null;
  /** Newest first (server order); null = not loaded yet. */
  runs: ChangeRun[] | null;
  /** Loaded change entries per `entriesKey(runId, stepId)`. */
  entries: Record<string, ChangeEntry[]>;
  error: string | null;
  /** Panel UI state that outlives the panel (phones unmount it on a tab switch). */
  expanded: Record<string, boolean>;
  /** The run whose diff layer was last highlighted, with the view highlight seq it set. */
  diffLayer: { runId: string; seq: number } | null;
  setExpanded: (runId: string, open: boolean) => void;
  setDiffLayer: (layer: { runId: string; seq: number } | null) => void;
  load: (projectId: string) => Promise<void>;
  loadEntries: (runId: string, stepId?: string | null) => Promise<void>;
  /**
   * A mounted view of the store (the panel): refetches on frames only while one is attached;
   * otherwise changed runs are remembered and refetched by the next `load()`.
   */
  attach: () => () => void;
  dispose: () => void;
}

const DEFAULT_DEPS: ChangesDeps = {
  fetchRuns: (projectId) => fetchChangeRuns(projectId),
  fetchEntries: (projectId, runId, stepId) => fetchChanges(projectId, runId, stepId),
};

/** The change log of the open project (E-C13); refreshes on `changes-updated` and after `run-finished`. */
export function createChangesStore(
  ws: Pick<WsClient, 'onFrame'>,
  deps: ChangesDeps = DEFAULT_DEPS,
): StoreApi<ChangesState> {
  // Bumped per project switch: responses of an older project are dropped.
  let generation = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const dirtyRuns = new Set<string>();
  // Latest request wins per key: an older response landing after a newer one is dropped.
  const latest = new Map<string, number>();
  let requestSeq = 0;
  const begin = (key: string) => {
    const id = ++requestSeq;
    latest.set(key, id);
    return () => latest.get(key) === id;
  };

  const fetchRuns = async () => {
    const { projectId } = store.getState();
    if (!projectId) return;
    const gen = generation;
    const current = begin('runs');
    try {
      const runs = await deps.fetchRuns(projectId);
      if (gen === generation && current()) store.setState({ runs, error: null });
    } catch (e) {
      if (gen === generation && current()) store.setState({ error: `Could not load the changes: ${errorText(e)}` });
    }
  };

  const fetchEntries = async (runId: string, stepId: string | null) => {
    const { projectId } = store.getState();
    if (!projectId) return;
    const gen = generation;
    const key = entriesKey(runId, stepId);
    const current = begin(key);
    try {
      const list = await deps.fetchEntries(projectId, runId, stepId);
      if (gen === generation && current()) store.setState((s) => ({ entries: { ...s.entries, [key]: list }, error: null }));
    } catch (e) {
      if (gen === generation && current()) store.setState({ error: `Could not load the changes: ${errorText(e)}` });
    }
  };

  let attached = 0;
  /** Refetches the runs and the loaded entry lists (run and steps) of the changed runs. */
  const flush = (): Promise<void> => {
    const runs = [...dirtyRuns];
    dirtyRuns.clear();
    const keys = Object.keys(store.getState().entries);
    for (const runId of runs) {
      for (const key of keys) {
        if (key.startsWith(`${runId}|`)) void fetchEntries(runId, key.slice(runId.length + 1) || null);
      }
    }
    return fetchRuns();
  };
  const schedule = (runId: string | null) => {
    if (runId) dirtyRuns.add(runId);
    if (attached === 0) return; // the next load() refetches
    clearTimeout(timer);
    timer = setTimeout(() => void flush(), CHANGES_DEBOUNCE_MS);
  };

  const store = createStore<ChangesState>()(() => ({
    projectId: null,
    runs: null,
    entries: {},
    error: null,
    expanded: {},
    diffLayer: null,
    setExpanded: (runId, open) => store.setState((s) => ({ expanded: { ...s.expanded, [runId]: open } })),
    setDiffLayer: (diffLayer) => store.setState({ diffLayer }),
    load: async (projectId) => {
      if (projectId !== store.getState().projectId) {
        generation += 1;
        clearTimeout(timer);
        dirtyRuns.clear();
        store.setState({ projectId, runs: null, entries: {}, error: null, expanded: {}, diffLayer: null });
      }
      clearTimeout(timer);
      await flush();
    },
    attach: () => {
      attached += 1;
      return () => {
        attached -= 1;
      };
    },
    loadEntries: (runId, stepId = null) => fetchEntries(runId, stepId),
    dispose: () => {
      clearTimeout(timer);
      offFrame();
    },
  }));

  const offFrame = ws.onFrame((f) => {
    if (f.type !== 'changes-updated' && f.type !== 'run-finished') return;
    if (f.projectId !== store.getState().projectId) return;
    schedule(f.runId || null);
  });
  return store;
}

export const changesStore = createChangesStore(wsClient);
export const ChangesStoreContext = createContext<StoreApi<ChangesState> | null>(null);

export function useChanges<T>(selector: (s: ChangesState) => T): T {
  return useStore(useContext(ChangesStoreContext) ?? changesStore, selector);
}
