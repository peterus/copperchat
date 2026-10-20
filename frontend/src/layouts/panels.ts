import { type ComponentType, useSyncExternalStore } from 'react';

/** Props every registered panel component receives. */
export interface PanelProps {
  projectId: string;
  /** From `useUi().openPanel(id, params)` (S-C8); absent when opened from the header or a tab. */
  params?: Record<string, string>;
}

/**
 * A side panel contributed by a later phase (index C7a Frontend). Register it from a module
 * imported by `src/panels/index.ts`. Reserved ids: `history` (Phase 7, also the iPhone
 * "History" tab), `git` (Phase 7), `skills`, `parts` (Phase 8). `icon` is a lucide-react icon.
 */
export interface PanelDefinition {
  id: string;
  title: string;
  icon: ComponentType<{ className?: string }>;
  component: ComponentType<PanelProps>;
  /** Ascending; default 100; ties sorted by title. */
  order?: number;
  /** The desktop / tablet sheet is wide (`max-w-3xl`) instead of `max-w-md` (e.g. Docs). */
  wide?: boolean;
}

const DEFAULT_ORDER = 100;
const registry = new Map<string, PanelDefinition>();
let snapshot: PanelDefinition[] = [];
const listeners = new Set<() => void>();

function publish(): void {
  snapshot = [...registry.values()].sort(
    (a, b) => (a.order ?? DEFAULT_ORDER) - (b.order ?? DEFAULT_ORDER) || a.title.localeCompare(b.title),
  );
  for (const l of [...listeners]) l();
}

/** Registering an existing id replaces it. Returns an unregister function (no-op once replaced). */
export function registerPanel(def: PanelDefinition): () => void {
  registry.set(def.id, def);
  publish();
  return () => {
    if (registry.get(def.id) !== def) return;
    registry.delete(def.id);
    publish();
  };
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

const getSnapshot = () => snapshot;

/** Registered panels, sorted by order then title; re-renders on (un)registration. */
export function usePanels(): PanelDefinition[] {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
