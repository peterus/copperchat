# Phase 6B: Frontend viewer

> **Cross-phase corrections apply:** see index § "Cross-phase corrections" (X1, X2, X3, X7); they supersede conflicting code in this file.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 6A `ViewerSlot` stub with the real viewer: Schematic/PCB tabs, a "KiCad view" (KiCanvas, or the own-viewer fallback) vs. "Agent view" (server PNG) toggle, eight PCB layer toggles for the agent view, reload on `view-updated`, tap on a component → `@REF` in the composer, and a fullscreen mode with pinch zoom on iPhone.

**Depends on:** 1B (decision in `docs/spikes/1b-kicanvas.md`), 4 (`GET /projects/{id}/view/{doc}`, `GET /projects/{id}/agent-view`), 6A (shell, composer, ws client, ui store).

**Parallel with:** 7, 8.

**Read first:** the index `docs/superpowers/plans/2026-10-15-foundation-plan.md` (Global Constraints, Go/no-go gates "1B", C6, C7 `view-updated`, C7a Frontend names, C8); spec §4.2, §11, §12; `docs/spikes/1b-kicanvas.md` (Decision, "Follow-ups for later phases": event channel, viewer lookup path, reference path, theme mapping, fonts, reload behaviour); Phase 6A file section "Interfaces" of Tasks 6A.2, 6A.8, 6A.11, 6A.12.

**Path selection (binding):** read the Decision in `docs/spikes/1b-kicanvas.md`.
- **1B go** → implement Tasks 6B.1–6B.6. Skip 6B.F1–6B.F3.
- **1B no-go** → implement Tasks 6B.1, 6B.2, 6B.4, 6B.5, 6B.F1, 6B.F2, 6B.F3 (6B.F3 provides `KicadPane` instead of 6B.3, and 6B.6 is skipped). Both paths export the same `KicadPane` component (`KicadPaneProps`), so Tasks 6B.4/6B.5 are identical for both.

**PR title:** `foundation phase 6B: viewer with KiCanvas, agent view and @REF picking` (no-go: `foundation phase 6B: own viewer with hit-test, agent view and @REF picking`)

**Files:**
- Create: `frontend/src/lib/viewUrls.ts`, `frontend/src/lib/viewUrls.test.ts`
- Create: `frontend/src/stores/view.ts`, `frontend/src/stores/view.test.ts`
- Create: `frontend/src/lib/panZoom.ts`, `frontend/src/lib/panZoom.test.ts`, `frontend/src/hooks/usePanZoom.ts`
- Create: `frontend/src/components/viewer/KicadPane.tsx` (path-specific re-export), `frontend/src/components/viewer/types.ts`
- Create: `frontend/src/components/viewer/AgentViewPane.tsx`, `frontend/src/components/viewer/LayerToggles.tsx`, `frontend/src/components/viewer/Viewer.tsx`, `frontend/src/components/viewer/Viewer.test.tsx`
- Modify: `frontend/src/components/viewer/ViewerSlot.tsx` (renders `Viewer`)
- Go path: Create `frontend/scripts/vendor-kicanvas.sh`, `frontend/scripts/check-kicanvas.sh`, `frontend/public/vendor/kicanvas/{kicanvas.js,LICENSE.md,BUILD_INFO,KICANVAS_COMMIT}`, `frontend/src/lib/kicanvas.ts`, `frontend/src/lib/kicanvas.test.ts`, `frontend/src/components/viewer/KicanvasPane.tsx`, `frontend/src/components/viewer/KicanvasPane.test.tsx`; Modify `frontend/eslint.config.js` (ignore `public/vendor`), `README.md` (third-party notice)
- No-go path: Create `backend/src/copperchat/views/hit_test.py`, `backend/tests/unit/test_hit_test.py`, `backend/tests/kicad/test_hit_test_kicad.py`, `frontend/src/components/viewer/OwnViewerPane.tsx`, `frontend/src/components/viewer/OwnViewerPane.test.tsx`; Modify `backend/src/copperchat/web/routes_views.py`

**Acceptance criteria:**
- `cd frontend && npm ci && npm run typecheck && npm run lint && npm test -- --run && npm run build` passes.
- Vitest covers: tab switching; `view-updated` version bump reloads the KiCad pane and the agent image (new `?v=`); `view-updated` for another project is ignored; mention insertion (`@R12 `) from a synthetic select event; layer toggles change the agent-view URL; pan/zoom math (wheel zoom about cursor, pinch, clamping, client→content mapping).
- Go path: `frontend/public/vendor/kicanvas/kicanvas.js` is built from the commit in `KICANVAS_COMMIT`, `LICENSE.md` (MIT) sits next to it, and `check-kicanvas.sh` proves the bundle contains no `fonts.googleapis.com`/`fonts.gstatic.com` reference.
- No-go path: `GET /api/projects/{id}/hit-test?doc=&x_mm=&y_mm=` → `{"ref": "<REF>" | null}`; `agent-view?mode=kicad` responses carry `X-CopperChat-Viewbox: <x_mm>,<y_mm>,<w_mm>,<h_mm>`; `uv run pytest -m kicad tests/kicad/test_hit_test_kicad.py` passes.
- The UI never says "Claude Code".

**Verification commands:**
```bash
cd frontend && npm ci && npm run typecheck && npm run lint && npm test -- --run && npm run build
cd frontend && ./scripts/check-kicanvas.sh                                    # go path
cd backend && uv sync && uv run pytest -m "not kicad" && uv run ruff check . && uv run ruff format --check . && uv run mypy src   # no-go path
cd backend && uv run pytest -m kicad tests/kicad/test_hit_test_kicad.py         # no-go path, needs kicad-cli api-server
```

**CI expectations:** `frontend` job green; backend job green; on the no-go path the `kicad` job (Phase 4) runs the new kicad test.

---

### Task 6B.1: View URL builders and view store

**Files:**
- Create: `frontend/src/lib/viewUrls.ts`, `frontend/src/stores/view.ts`
- Test: `frontend/src/lib/viewUrls.test.ts`, `frontend/src/stores/view.test.ts`

**Interfaces:**
- Consumes: `apiUrl(path)` from `@/api/http`; `WsClient`, `wsClient` from `@/api/ws`; `ServerFrame` from `@/protocol/frames`; `ResolvedTheme` from `@/stores/ui`.
- Produces:
  - `type DocKind = 'sch' | 'pcb'`; `type ViewMode = 'kicad' | 'agent'`
  - `PCB_LAYERS: readonly ['F.Cu','B.Cu','F.SilkS','B.SilkS','Edge.Cuts','F.Fab','B.Fab','User.Drawings']`; `type PcbLayer`; `DEFAULT_LAYERS: PcbLayer[]`
  - `snapshotUrl(projectId: string, doc: DocKind, version: number | null): string`
  - `agentViewUrl(o: {projectId: string; doc: DocKind; theme: ResolvedTheme; layers: readonly PcbLayer[]; version: number | null; mode?: 'agent' | 'kicad'}): string`
  - `createViewStore(ws: Pick<WsClient, 'onFrame'>): StoreApi<ViewState>`; `viewStore`; `ViewStoreContext`; `useView<T>(selector: (s: ViewState) => T): T`
  - `ViewState { projectId: string | null; versions: Record<DocKind, number | null>; doc: DocKind; mode: ViewMode; layers: PcbLayer[]; fullscreen: boolean; setProject(id: string): void; setDoc(d: DocKind): void; setMode(m: ViewMode): void; toggleLayer(l: PcbLayer): void; setFullscreen(on: boolean): void; dispose(): void }`

Version semantics: `version === null` means "no `view-updated` seen since the project was opened"; the URL then has no `v` parameter (Phase 4 serves snapshots with `ETag` and `Cache-Control: no-cache`, so the first load revalidates). Each `view-updated` sets `versions[doc] = max(current, frame.version)` for every doc in `frame.docs`, only for the current project.

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/viewUrls.test.ts`:
```ts
import { describe, expect, it } from 'vitest';
import { agentViewUrl, DEFAULT_LAYERS, PCB_LAYERS, snapshotUrl } from './viewUrls';

describe('snapshotUrl', () => {
  it('adds the version as cache buster', () => {
    expect(snapshotUrl('p 1', 'pcb', 4)).toBe('/api/projects/p%201/view/pcb?v=4');
  });
  it('omits v before the first view-updated', () => {
    expect(snapshotUrl('p1', 'sch', null)).toBe('/api/projects/p1/view/sch');
  });
});

describe('agentViewUrl', () => {
  it('builds a pcb URL with comma-separated layers in canonical order', () => {
    expect(agentViewUrl({ projectId: 'p1', doc: 'pcb', theme: 'dark', layers: ['B.Cu', 'F.Cu', 'Edge.Cuts'], version: 7 }))
      .toBe('/api/projects/p1/agent-view?doc=pcb&theme=dark&layers=F.Cu,B.Cu,Edge.Cuts&v=7');
  });
  it('drops layers for the schematic', () => {
    expect(agentViewUrl({ projectId: 'p1', doc: 'sch', theme: 'light', layers: ['F.Cu'], version: null }))
      .toBe('/api/projects/p1/agent-view?doc=sch&theme=light');
  });
  it('passes mode=kicad through', () => {
    expect(agentViewUrl({ projectId: 'p1', doc: 'sch', theme: 'light', layers: [], version: 2, mode: 'kicad' }))
      .toBe('/api/projects/p1/agent-view?doc=sch&theme=light&mode=kicad&v=2');
  });
  it('has eight layers and sensible defaults', () => {
    expect(PCB_LAYERS).toHaveLength(8);
    expect(DEFAULT_LAYERS).toEqual(['F.Cu', 'B.Cu', 'F.SilkS', 'B.SilkS', 'Edge.Cuts']);
  });
});
```

`frontend/src/stores/view.test.ts`:
```ts
import { beforeEach, describe, expect, it } from 'vitest';
import { FakeWsClient } from '@/test/fakeWsClient';
import { createViewStore } from './view';

let ws: FakeWsClient;
let store: ReturnType<typeof createViewStore>;

beforeEach(() => {
  ws = new FakeWsClient();
  store = createViewStore(ws);
  store.getState().setProject('p1');
});

describe('view store', () => {
  it('bumps the version per doc on view-updated', () => {
    ws.emit({ type: 'view-updated', projectId: 'p1', docs: ['pcb'], version: 3 });
    expect(store.getState().versions).toEqual({ sch: null, pcb: 3 });
    ws.emit({ type: 'view-updated', projectId: 'p1', docs: ['sch', 'pcb'], version: 5 });
    expect(store.getState().versions).toEqual({ sch: 5, pcb: 5 });
  });
  it('never goes backwards (replayed frame)', () => {
    ws.emit({ type: 'view-updated', projectId: 'p1', docs: ['pcb'], version: 5 });
    ws.emit({ type: 'view-updated', projectId: 'p1', docs: ['pcb'], version: 4 });
    expect(store.getState().versions.pcb).toBe(5);
  });
  it('ignores other projects', () => {
    ws.emit({ type: 'view-updated', projectId: 'p2', docs: ['pcb'], version: 9 });
    expect(store.getState().versions.pcb).toBeNull();
  });
  it('resets versions and fullscreen when switching projects', () => {
    ws.emit({ type: 'view-updated', projectId: 'p1', docs: ['pcb'], version: 2 });
    store.getState().setFullscreen(true);
    store.getState().setProject('p2');
    expect(store.getState().versions).toEqual({ sch: null, pcb: null });
    expect(store.getState().fullscreen).toBe(false);
  });
  it('toggles layers keeping canonical order', () => {
    store.getState().toggleLayer('F.Cu');
    expect(store.getState().layers).toEqual(['B.Cu', 'F.SilkS', 'B.SilkS', 'Edge.Cuts']);
    store.getState().toggleLayer('B.Fab');
    store.getState().toggleLayer('F.Cu');
    expect(store.getState().layers).toEqual(['F.Cu', 'B.Cu', 'F.SilkS', 'B.SilkS', 'Edge.Cuts', 'B.Fab']);
  });
  it('stops listening after dispose', () => {
    store.getState().dispose();
    ws.emit({ type: 'view-updated', projectId: 'p1', docs: ['pcb'], version: 3 });
    expect(store.getState().versions.pcb).toBeNull();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/lib/viewUrls.test.ts src/stores/view.test.ts`
Expected: FAIL — `Failed to resolve import "./viewUrls"` / `"./view"`.

- [ ] **Step 3: Write minimal implementation**

`frontend/src/lib/viewUrls.ts`:
```ts
import { apiUrl } from '@/api/http';
import type { ResolvedTheme } from '@/stores/ui';

export type DocKind = 'sch' | 'pcb';
export type ViewMode = 'kicad' | 'agent';

export const PCB_LAYERS = ['F.Cu', 'B.Cu', 'F.SilkS', 'B.SilkS', 'Edge.Cuts', 'F.Fab', 'B.Fab', 'User.Drawings'] as const;
export type PcbLayer = (typeof PCB_LAYERS)[number];
export const DEFAULT_LAYERS: PcbLayer[] = ['F.Cu', 'B.Cu', 'F.SilkS', 'B.SilkS', 'Edge.Cuts'];

export function canonicalLayers(layers: readonly PcbLayer[]): PcbLayer[] {
  return PCB_LAYERS.filter((l) => layers.includes(l));
}

const project = (id: string) => `/projects/${encodeURIComponent(id)}`;

export function snapshotUrl(projectId: string, doc: DocKind, version: number | null): string {
  const base = apiUrl(`${project(projectId)}/view/${doc}`);
  return version === null ? base : `${base}?v=${version}`;
}

export function agentViewUrl(o: {
  projectId: string; doc: DocKind; theme: ResolvedTheme; layers: readonly PcbLayer[];
  version: number | null; mode?: 'agent' | 'kicad';
}): string {
  const q = [`doc=${o.doc}`, `theme=${o.theme}`];
  if (o.doc === 'pcb' && o.mode !== 'kicad') {
    q.push(`layers=${canonicalLayers(o.layers).map(encodeURIComponent).join(',')}`);
  }
  if (o.mode === 'kicad') q.push('mode=kicad');
  if (o.version !== null) q.push(`v=${o.version}`);
  return `${apiUrl(`${project(o.projectId)}/agent-view`)}?${q.join('&')}`;
}
```

`frontend/src/stores/view.ts`:
```ts
import { createContext, useContext } from 'react';
import { createStore, type StoreApi, useStore } from 'zustand';
import { type WsClient, wsClient } from '@/api/ws';
import { canonicalLayers, DEFAULT_LAYERS, type DocKind, type PcbLayer, type ViewMode } from '@/lib/viewUrls';

export interface ViewState {
  projectId: string | null;
  versions: Record<DocKind, number | null>;
  doc: DocKind;
  mode: ViewMode;
  layers: PcbLayer[];
  fullscreen: boolean;
  setProject: (id: string) => void;
  setDoc: (d: DocKind) => void;
  setMode: (m: ViewMode) => void;
  toggleLayer: (l: PcbLayer) => void;
  setFullscreen: (on: boolean) => void;
  dispose: () => void;
}

export function createViewStore(ws: Pick<WsClient, 'onFrame'>): StoreApi<ViewState> {
  let off: () => void = () => {};
  const store = createStore<ViewState>()((set, get) => ({
    projectId: null,
    versions: { sch: null, pcb: null },
    doc: 'sch',
    mode: 'kicad',
    layers: [...DEFAULT_LAYERS],
    fullscreen: false,
    setProject: (id) => {
      if (get().projectId === id) return;
      set({ projectId: id, versions: { sch: null, pcb: null }, fullscreen: false });
    },
    setDoc: (doc) => set({ doc }),
    setMode: (mode) => set({ mode }),
    toggleLayer: (l) => {
      const cur = get().layers;
      set({ layers: canonicalLayers(cur.includes(l) ? cur.filter((x) => x !== l) : [...cur, l]) });
    },
    setFullscreen: (fullscreen) => set({ fullscreen }),
    dispose: () => off(),
  }));
  off = ws.onFrame((f) => {
    if (f.type !== 'view-updated') return;
    const s = store.getState();
    if (f.projectId !== s.projectId) return;
    const versions = { ...s.versions };
    for (const d of f.docs) versions[d] = Math.max(versions[d] ?? -1, f.version);
    store.setState({ versions });
  });
  return store;
}

export const viewStore = createViewStore(wsClient);
export const ViewStoreContext = createContext<StoreApi<ViewState> | null>(null);

export function useView<T>(selector: (s: ViewState) => T): T {
  return useStore(useContext(ViewStoreContext) ?? viewStore, selector);
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/lib/viewUrls.test.ts src/stores/view.test.ts && npm run typecheck && npm run lint`
Expected: PASS (11 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/viewUrls.ts frontend/src/lib/viewUrls.test.ts frontend/src/stores/view.ts frontend/src/stores/view.test.ts
git commit -m "feat(frontend): view store with per-doc versions and view URL builders

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6B.2: Pan/zoom math and hook

**Files:**
- Create: `frontend/src/lib/panZoom.ts`, `frontend/src/hooks/usePanZoom.ts`
- Test: `frontend/src/lib/panZoom.test.ts`

**Interfaces:**
- Produces (all pure, coordinates in CSS px relative to the container; transform is `translate(x,y) scale(scale)` with origin `0 0`):
  - `interface PanZoomView { scale: number; x: number; y: number }`; `IDENTITY: PanZoomView`
  - `interface Limits { min: number; max: number }`; `DEFAULT_LIMITS = { min: 1, max: 12 }`
  - `clampScale(s: number, l?: Limits): number`
  - `wheelFactor(deltaY: number): number`
  - `zoomAt(v: PanZoomView, factor: number, cx: number, cy: number, l?: Limits): PanZoomView`
  - `pan(v: PanZoomView, dx: number, dy: number): PanZoomView`
  - `distance(a: Pt, b: Pt): number`; `midpoint(a: Pt, b: Pt): Pt`
  - `pinch(start: {view: PanZoomView; a: Pt; b: Pt}, a: Pt, b: Pt, l?: Limits): PanZoomView`
  - `toContent(v: PanZoomView, px: number, py: number): Pt`
  - `usePanZoom(): { view: PanZoomView; reset(): void; bind: { onWheel, onPointerDown, onPointerMove, onPointerUp, onPointerCancel }; style: CSSProperties; moved(): boolean }`

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/panZoom.test.ts`:
```ts
import { describe, expect, it } from 'vitest';
import { clampScale, distance, IDENTITY, midpoint, pan, pinch, toContent, wheelFactor, zoomAt } from './panZoom';

describe('pan/zoom math', () => {
  it('clamps the scale', () => {
    expect(clampScale(0.5)).toBe(1);
    expect(clampScale(50)).toBe(12);
    expect(clampScale(3, { min: 0.5, max: 4 })).toBe(3);
  });

  it('wheel up zooms in, wheel down zooms out, zero is neutral', () => {
    expect(wheelFactor(-100)).toBeGreaterThan(1);
    expect(wheelFactor(100)).toBeLessThan(1);
    expect(wheelFactor(0)).toBe(1);
    expect(wheelFactor(-100) * wheelFactor(100)).toBeCloseTo(1, 10);
  });

  it('zooms about the cursor: the content point under the cursor stays put', () => {
    const v0 = { scale: 2, x: -50, y: -20 };
    const before = toContent(v0, 120, 80);
    const v1 = zoomAt(v0, 1.5, 120, 80);
    expect(v1.scale).toBe(3);
    const after = toContent(v1, 120, 80);
    expect(after.x).toBeCloseTo(before.x, 10);
    expect(after.y).toBeCloseTo(before.y, 10);
  });

  it('does not move when the scale is already at a limit', () => {
    const v = zoomAt({ scale: 12, x: -10, y: -10 }, 2, 50, 50);
    expect(v).toEqual({ scale: 12, x: -10, y: -10 });
  });

  it('pans by a delta', () => {
    expect(pan(IDENTITY, 5, -3)).toEqual({ scale: 1, x: 5, y: -3 });
  });

  it('pinch scales by the finger distance ratio and keeps the midpoint anchored', () => {
    const start = { view: IDENTITY, a: { x: 100, y: 100 }, b: { x: 200, y: 100 } };
    const v = pinch(start, { x: 50, y: 100 }, { x: 250, y: 100 });
    expect(v.scale).toBeCloseTo(2, 10);
    const anchor = toContent(v, 150, 100);
    expect(anchor.x).toBeCloseTo(150, 10);
    expect(anchor.y).toBeCloseTo(100, 10);
  });

  it('pinch follows a moving midpoint (two-finger pan)', () => {
    const start = { view: IDENTITY, a: { x: 0, y: 0 }, b: { x: 100, y: 0 } };
    const v = pinch(start, { x: 30, y: 40 }, { x: 130, y: 40 });
    expect(v).toEqual({ scale: 1, x: 30, y: 40 });
  });

  it('pinch respects limits', () => {
    const start = { view: IDENTITY, a: { x: 0, y: 0 }, b: { x: 100, y: 0 } };
    expect(pinch(start, { x: 0, y: 0 }, { x: 10, y: 0 }).scale).toBe(1);
  });

  it('distance and midpoint', () => {
    expect(distance({ x: 0, y: 0 }, { x: 3, y: 4 })).toBe(5);
    expect(midpoint({ x: 0, y: 2 }, { x: 4, y: 6 })).toEqual({ x: 2, y: 4 });
  });

  it('maps client px to content px', () => {
    expect(toContent({ scale: 2, x: 10, y: 20 }, 50, 60)).toEqual({ x: 20, y: 20 });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/lib/panZoom.test.ts`
Expected: FAIL — `Failed to resolve import "./panZoom"`.

- [ ] **Step 3: Write minimal implementation**

`frontend/src/lib/panZoom.ts`:
```ts
export interface PanZoomView { scale: number; x: number; y: number }
export interface Limits { min: number; max: number }
export interface Pt { x: number; y: number }

export const IDENTITY: PanZoomView = { scale: 1, x: 0, y: 0 };
export const DEFAULT_LIMITS: Limits = { min: 1, max: 12 };

export function clampScale(s: number, l: Limits = DEFAULT_LIMITS): number {
  return Math.min(l.max, Math.max(l.min, s));
}

export function wheelFactor(deltaY: number): number {
  return Math.exp(-deltaY * 0.0015);
}

export function zoomAt(v: PanZoomView, factor: number, cx: number, cy: number, l: Limits = DEFAULT_LIMITS): PanZoomView {
  const scale = clampScale(v.scale * factor, l);
  const k = scale / v.scale;
  return { scale, x: cx - (cx - v.x) * k, y: cy - (cy - v.y) * k };
}

export function pan(v: PanZoomView, dx: number, dy: number): PanZoomView {
  return { scale: v.scale, x: v.x + dx, y: v.y + dy };
}

export function distance(a: Pt, b: Pt): number {
  return Math.hypot(b.x - a.x, b.y - a.y);
}

export function midpoint(a: Pt, b: Pt): Pt {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

export function pinch(start: { view: PanZoomView; a: Pt; b: Pt }, a: Pt, b: Pt, l: Limits = DEFAULT_LIMITS): PanZoomView {
  const d0 = distance(start.a, start.b);
  const factor = d0 === 0 ? 1 : distance(a, b) / d0;
  const scale = clampScale(start.view.scale * factor, l);
  const k = scale / start.view.scale;
  const m0 = midpoint(start.a, start.b);
  const m1 = midpoint(a, b);
  return { scale, x: m1.x - (m0.x - start.view.x) * k, y: m1.y - (m0.y - start.view.y) * k };
}

export function toContent(v: PanZoomView, px: number, py: number): Pt {
  return { x: (px - v.x) / v.scale, y: (py - v.y) / v.scale };
}
```

`frontend/src/hooks/usePanZoom.ts`:
```ts
import { type CSSProperties, type PointerEvent, useCallback, useRef, useState, type WheelEvent } from 'react';
import { IDENTITY, pan, type PanZoomView, pinch, type Pt, wheelFactor, zoomAt } from '@/lib/panZoom';

const TAP_SLOP_PX = 6;

export function usePanZoom() {
  const [view, setView] = useState<PanZoomView>(IDENTITY);
  const pointers = useRef(new Map<number, Pt>());
  const pinchStart = useRef<{ view: PanZoomView; a: Pt; b: Pt } | null>(null);
  const travel = useRef(0);
  const viewRef = useRef(view);
  viewRef.current = view;

  const local = (e: { clientX: number; clientY: number; currentTarget: Element }): Pt => {
    const r = e.currentTarget.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  const reset = useCallback(() => setView(IDENTITY), []);

  const bind = {
    onWheel: (e: WheelEvent<HTMLElement>) => {
      const p = local(e);
      setView((v) => zoomAt(v, wheelFactor(e.deltaY), p.x, p.y));
    },
    onPointerDown: (e: PointerEvent<HTMLElement>) => {
      e.currentTarget.setPointerCapture?.(e.pointerId);
      pointers.current.set(e.pointerId, local(e));
      if (pointers.current.size === 1) travel.current = 0;
      if (pointers.current.size === 2) {
        const [a, b] = [...pointers.current.values()];
        pinchStart.current = { view: viewRef.current, a, b };
      }
    },
    onPointerMove: (e: PointerEvent<HTMLElement>) => {
      const prev = pointers.current.get(e.pointerId);
      if (!prev) return;
      const p = local(e);
      pointers.current.set(e.pointerId, p);
      travel.current += Math.hypot(p.x - prev.x, p.y - prev.y);
      if (pointers.current.size >= 2 && pinchStart.current) {
        const [a, b] = [...pointers.current.values()];
        setView(pinch(pinchStart.current, a, b));
      } else if (pointers.current.size === 1) {
        setView((v) => pan(v, p.x - prev.x, p.y - prev.y));
      }
    },
    onPointerUp: (e: PointerEvent<HTMLElement>) => {
      pointers.current.delete(e.pointerId);
      if (pointers.current.size < 2) pinchStart.current = null;
    },
    onPointerCancel: (e: PointerEvent<HTMLElement>) => {
      pointers.current.delete(e.pointerId);
      pinchStart.current = null;
    },
  };

  const style: CSSProperties = {
    transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})`,
    transformOrigin: '0 0',
    touchAction: 'none',
  };

  /** True when the last gesture moved more than a tap; click handlers use it to ignore drags. */
  const moved = useCallback(() => travel.current > TAP_SLOP_PX, []);

  return { view, reset, bind, style, moved };
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/lib/panZoom.test.ts && npm run typecheck && npm run lint`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/panZoom.ts frontend/src/lib/panZoom.test.ts frontend/src/hooks/usePanZoom.ts
git commit -m "feat(frontend): pan/zoom math (wheel, pinch, clamping) and usePanZoom hook

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6B.3: Vendored KiCanvas and KicanvasPane (1B go only)

**Files:**
- Create: `frontend/scripts/vendor-kicanvas.sh`, `frontend/scripts/check-kicanvas.sh`, `frontend/public/vendor/kicanvas/KICANVAS_COMMIT` (+ generated `kicanvas.js`, `LICENSE.md`, `BUILD_INFO`)
- Create: `frontend/src/lib/kicanvas.ts`, `frontend/src/components/viewer/types.ts`, `frontend/src/components/viewer/KicanvasPane.tsx`, `frontend/src/components/viewer/KicadPane.tsx`
- Modify: `frontend/eslint.config.js` (add `public/vendor/**` to `ignores`), `README.md` (Third-party: "KiCanvas (MIT), vendored in frontend/public/vendor/kicanvas")
- Test: `frontend/src/lib/kicanvas.test.ts`, `frontend/src/components/viewer/KicanvasPane.test.tsx`

**Interfaces:**
- Consumes: `useComposer().insertText(text)` from `@/stores/composer` (inserts with spacing: `insertText('@R12')` on an empty composer yields `'@R12 '`); `snapshotUrl` (6B.1).
- Produces:
  - `KicadPaneProps { projectId: string; doc: DocKind; version: number | null; theme: ResolvedTheme; onPick: (ref: string) => void }` in `@/components/viewer/types`
  - `KicadPane: (p: KicadPaneProps) => JSX.Element` from `@/components/viewer/KicadPane` (both paths)
  - `insertMention(ref: string): void` in `@/components/viewer/types` (calls `useComposer.getState().insertText('@' + ref)`)
  - `@/lib/kicanvas`: `loadKicanvas(): Promise<void>`, `findViewer(root: Element): EventTarget | null`, `refOf(item: unknown): string | null`, `attachSelect(embed: HTMLElement, onRef: (ref: string) => void): () => void`, `kicanvasTheme(t: ResolvedTheme): string`

Use the values recorded in `docs/spikes/1b-kicanvas.md` ("Follow-ups for later phases"): pinned commit, viewer lookup path, reference extraction path, channel(s), theme names. The code below implements the spike page's lookup (`node.viewer` through shadow roots) and reference paths; if the doc records a different property path or theme mapping, use that in `findViewer` / `refOf` / `kicanvasTheme` and keep the tests' fake objects in sync.

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/kicanvas.test.ts`:
```ts
import { describe, expect, it, vi } from 'vitest';
import { attachSelect, findViewer, kicanvasTheme, refOf } from './kicanvas';

const selectEvent = (item: unknown) => new CustomEvent('kicanvas:select', { detail: { item } });

describe('refOf', () => {
  it('reads reference from the known shapes and walks parents', () => {
    expect(refOf({ reference: 'R12' })).toBe('R12');
    expect(refOf({ properties: new Map([['Reference', { text: 'C3' }]]) })).toBe('C3');
    expect(refOf({ properties: [{ name: 'Reference', value: 'U1' }] })).toBe('U1');
    expect(refOf({ parent: { parent: { ref: 'Q2' } } })).toBe('Q2');
    expect(refOf({ parent: null })).toBeNull();
    expect(refOf(undefined)).toBeNull();
  });
});

describe('findViewer / attachSelect', () => {
  function embedWithShadowViewer() {
    const embed = document.createElement('div');
    const shadow = embed.attachShadow({ mode: 'open' });
    const inner = document.createElement('div');
    shadow.appendChild(inner);
    const viewer = new EventTarget();
    document.body.appendChild(embed);
    return { embed, inner, viewer };
  }

  it('finds an object with a viewer property inside shadow roots', () => {
    const { embed, inner, viewer } = embedWithShadowViewer();
    expect(findViewer(embed)).toBeNull();
    Object.assign(inner, { viewer });
    expect(findViewer(embed)).toBe(viewer);
  });

  it('reports refs from the viewer channel after kicanvas:load and from the DOM channel, deduplicated', () => {
    const { embed, inner, viewer } = embedWithShadowViewer();
    const onRef = vi.fn();
    const off = attachSelect(embed, onRef);
    Object.assign(inner, { viewer });
    embed.dispatchEvent(new Event('kicanvas:load'));
    viewer.dispatchEvent(selectEvent({ reference: 'R12' }));
    embed.dispatchEvent(selectEvent({ reference: 'R12' }));   // same click re-dispatched on the element
    expect(onRef).toHaveBeenCalledTimes(1);
    expect(onRef).toHaveBeenCalledWith('R12');
    viewer.dispatchEvent(selectEvent(null));                   // deselect
    expect(onRef).toHaveBeenCalledTimes(1);
    off();
    viewer.dispatchEvent(selectEvent({ reference: 'C1' }));
    expect(onRef).toHaveBeenCalledTimes(1);
  });

  it('maps the app theme to a KiCanvas theme', () => {
    expect(kicanvasTheme('dark')).toBe('witchhazel');
    expect(kicanvasTheme('light')).toBe('kicad');
  });
});
```

`frontend/src/components/viewer/KicanvasPane.test.tsx`:
```tsx
import { act, render } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useComposer } from '@/stores/composer';
import { KicadPane } from './KicadPane';
import { insertMention } from './types';

vi.mock('@/lib/kicanvas', async (orig) => ({
  ...(await orig<typeof import('@/lib/kicanvas')>()),
  loadKicanvas: () => Promise.resolve(),
}));

beforeEach(() => useComposer.getState().clear());

async function renderPane(version: number | null) {
  const utils = render(<KicadPane projectId="p1" doc="pcb" version={version} theme="dark" onPick={insertMention} />);
  await act(async () => {});
  return utils;
}

describe('KicanvasPane', () => {
  it('creates an embed with the snapshot source and theme', async () => {
    const { container } = await renderPane(3);
    const embed = container.querySelector('kicanvas-embed')!;
    expect(embed.getAttribute('theme')).toBe('witchhazel');
    expect(embed.getAttribute('controls')).toBe('basic');
    expect(embed.querySelector('kicanvas-source')!.getAttribute('src')).toBe('/api/projects/p1/view/pcb?v=3');
  });

  it('inserts @R12 into the composer from a synthetic select event', async () => {
    const { container } = await renderPane(1);
    const embed = container.querySelector('kicanvas-embed') as HTMLElement;
    const viewer = new EventTarget();
    Object.assign(embed, { viewer });
    act(() => { embed.dispatchEvent(new Event('kicanvas:load')); });
    act(() => {
      viewer.dispatchEvent(new CustomEvent('kicanvas:select',
        { detail: { item: { properties: new Map([['Reference', { text: 'R12' }]]) } } }));
    });
    expect(useComposer.getState().text).toBe('@R12 ');
  });

  it('re-creates the embed when the version bumps', async () => {
    const { container, rerender } = await renderPane(1);
    const first = container.querySelector('kicanvas-embed');
    rerender(<KicadPane projectId="p1" doc="pcb" version={2} theme="dark" onPick={insertMention} />);
    await act(async () => {});
    const second = container.querySelector('kicanvas-embed')!;
    expect(second).not.toBe(first);
    expect(second.querySelector('kicanvas-source')!.getAttribute('src')).toBe('/api/projects/p1/view/pcb?v=2');
  });
});
```

`frontend/scripts/check-kicanvas.sh`:
```bash
#!/usr/bin/env bash
# Asserts the vendored KiCanvas bundle is present, MIT licensed, pinned and offline (no Google Fonts).
set -euo pipefail
V="$(cd "$(dirname "$0")/.." && pwd)/public/vendor/kicanvas"
fail() { echo "FAIL: $*" >&2; exit 1; }
[ -s "$V/kicanvas.js" ] || fail "kicanvas.js missing"
[ -s "$V/KICANVAS_COMMIT" ] || fail "KICANVAS_COMMIT missing"
grep -q "MIT License" "$V/LICENSE.md" || fail "LICENSE.md is not the MIT license"
grep -q "commit=$(cat "$V/KICANVAS_COMMIT")" "$V/BUILD_INFO" || fail "BUILD_INFO does not match KICANVAS_COMMIT"
grep -q 'kicanvas-embed' "$V/kicanvas.js" || fail "bundle does not define kicanvas-embed"
if grep -Eq 'fonts\.(googleapis|gstatic)\.com' "$V/kicanvas.js"; then fail "bundle still loads Google Fonts"; fi
echo "PASS: kicanvas vendored ($(stat -c %s "$V/kicanvas.js") bytes, commit $(cat "$V/KICANVAS_COMMIT"))"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && chmod +x scripts/check-kicanvas.sh && ./scripts/check-kicanvas.sh; npx vitest run src/lib/kicanvas.test.ts src/components/viewer/KicanvasPane.test.tsx`
Expected: `FAIL: kicanvas.js missing` (exit 1) and vitest FAIL — `Failed to resolve import "./kicanvas"` / `"./KicadPane"`.

- [ ] **Step 3: Write minimal implementation**

`frontend/public/vendor/kicanvas/KICANVAS_COMMIT`: the full commit SHA recorded in `docs/spikes/1b-kicanvas.md` (one line, no other content).

`frontend/scripts/vendor-kicanvas.sh` (the spike 1B recipe plus font stripping):
```bash
#!/usr/bin/env bash
# Builds KiCanvas at the pinned commit and vendors it into public/vendor/kicanvas.
# KiCanvas injects a Google Fonts stylesheet; we strip it so the app works offline
# and makes no third-party requests (UI falls back to the system font stack).
set -euo pipefail
V="$(cd "$(dirname "$0")/.." && pwd)/public/vendor/kicanvas"
COMMIT="$(cat "$V/KICANVAS_COMMIT")"
SRC="$(mktemp -d)/kicanvas"
git clone --quiet https://github.com/theacodes/kicanvas.git "$SRC"
git -C "$SRC" checkout --quiet "$COMMIT"
(cd "$SRC" && npm ci --no-audit --no-fund && npm run build)
cp "$SRC/build/kicanvas.js" "$V/kicanvas.js"
cp "$SRC/LICENSE.md" "$V/LICENSE.md"
# Replace any @import/link of Google Fonts with nothing (keeps the bundle syntactically intact).
sed -i -E 's#@import url\((["'"'"']?)https://fonts\.googleapis\.com/[^)]*\);?##g; s#https://fonts\.(googleapis|gstatic)\.com/[^"'"'"'`)]*#data:text/css,#g' "$V/kicanvas.js"
{
  echo "commit=$COMMIT"
  echo "node=$(node --version) npm=$(npm --version)"
  echo "size=$(stat -c %s "$V/kicanvas.js")"
  echo "sha256=$(sha256sum "$V/kicanvas.js" | cut -d' ' -f1)"
  echo "patch=google-fonts-stripped"
} > "$V/BUILD_INFO"
cat "$V/BUILD_INFO"
```
Run it once: `cd frontend && chmod +x scripts/vendor-kicanvas.sh && ./scripts/vendor-kicanvas.sh`. If the spike recorded that the font URL is assembled differently (e.g. a `<link>` built at runtime), adapt the `sed` so `check-kicanvas.sh` passes and record the patch in `BUILD_INFO`.

`frontend/eslint.config.js` — extend the existing global ignores entry: `{ ignores: ['dist', 'public/vendor/**'] }`.

`frontend/src/lib/kicanvas.ts`:
```ts
import type { ResolvedTheme } from '@/stores/ui';

const SCRIPT_SRC = '/vendor/kicanvas/kicanvas.js';
let loading: Promise<void> | null = null;

export function loadKicanvas(): Promise<void> {
  if (!loading) {
    loading = new Promise<void>((resolve, reject) => {
      const s = document.createElement('script');
      s.type = 'module';
      s.src = SCRIPT_SRC;
      s.onerror = () => { loading = null; reject(new Error('KiCanvas failed to load')); };
      document.head.appendChild(s);
      void customElements.whenDefined('kicanvas-embed').then(() => resolve());
    });
  }
  return loading;
}

export function kicanvasTheme(t: ResolvedTheme): string {
  return t === 'dark' ? 'witchhazel' : 'kicad';
}

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null;
const textOf = (v: unknown): string | null => {
  if (typeof v === 'string') return v || null;
  if (isObj(v)) for (const k of ['text', 'value']) if (typeof v[k] === 'string' && v[k]) return v[k] as string;
  return null;
};

export function refOf(item: unknown, depth = 0): string | null {
  if (!isObj(item) || depth > 4) return null;
  for (const k of ['reference', 'ref']) if (typeof item[k] === 'string' && item[k]) return item[k] as string;
  const props = item.properties;
  if (props instanceof Map) {
    const r = textOf(props.get('Reference'));
    if (r) return r;
  } else if (Array.isArray(props)) {
    const p = props.find((x) => isObj(x) && x.name === 'Reference');
    const r = textOf(p);
    if (r) return r;
  }
  return refOf(item.parent, depth + 1);
}

export function findViewer(root: Element): EventTarget | null {
  const queue: Array<Element | ShadowRoot> = [root];
  while (queue.length) {
    const node = queue.shift()!;
    if (node instanceof Element) {
      const v = (node as unknown as Obj).viewer;
      if (v instanceof EventTarget || (isObj(v) && typeof v.addEventListener === 'function')) return v as EventTarget;
      if (node.shadowRoot) queue.push(node.shadowRoot);
    }
    for (const child of Array.from(node.children)) queue.push(child);
  }
  return null;
}

const DEDUPE_MS = 100;

export function attachSelect(embed: HTMLElement, onRef: (ref: string) => void): () => void {
  let last: { ref: string; at: number } | null = null;
  let viewer: EventTarget | null = null;
  const handle = (e: Event) => {
    const ref = refOf((e as CustomEvent<{ item?: unknown }>).detail?.item);
    if (!ref) return;
    const now = performance.now();
    if (last && last.ref === ref && now - last.at < DEDUPE_MS) return;
    last = { ref, at: now };
    onRef(ref);
  };
  const tryAttach = () => {
    if (viewer) return;
    viewer = findViewer(embed);
    viewer?.addEventListener('kicanvas:select', handle);
  };
  embed.addEventListener('kicanvas:select', handle);   // DOM channel (if the build re-dispatches)
  embed.addEventListener('kicanvas:load', tryAttach);
  tryAttach();
  return () => {
    embed.removeEventListener('kicanvas:select', handle);
    embed.removeEventListener('kicanvas:load', tryAttach);
    viewer?.removeEventListener('kicanvas:select', handle);
  };
}
```

`frontend/src/components/viewer/types.ts`:
```ts
import type { DocKind } from '@/lib/viewUrls';
import { useComposer } from '@/stores/composer';
import type { ResolvedTheme } from '@/stores/ui';

export interface KicadPaneProps {
  projectId: string;
  doc: DocKind;
  version: number | null;
  theme: ResolvedTheme;
  onPick: (ref: string) => void;
}

export function insertMention(ref: string): void {
  useComposer.getState().insertText(`@${ref}`);
}
```

`frontend/src/components/viewer/KicanvasPane.tsx`:
```tsx
import { useEffect, useRef, useState } from 'react';
import { attachSelect, kicanvasTheme, loadKicanvas } from '@/lib/kicanvas';
import { snapshotUrl } from '@/lib/viewUrls';
import type { KicadPaneProps } from './types';

export function KicanvasPane({ projectId, doc, version, theme, onPick }: KicadPaneProps) {
  const host = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const pick = useRef(onPick);
  pick.current = onPick;
  const src = snapshotUrl(projectId, doc, version);
  const kTheme = kicanvasTheme(theme);

  useEffect(() => {
    let cancelled = false;
    let detach: () => void = () => {};
    const el = host.current;
    void loadKicanvas().then(
      () => {
        if (cancelled || !el) return;
        // Re-create the element on every source/theme change: reliable reload (spike 1B hint).
        const embed = document.createElement('kicanvas-embed');
        embed.setAttribute('controls', 'basic');
        embed.setAttribute('theme', kTheme);
        embed.style.display = 'block';
        embed.style.width = '100%';
        embed.style.height = '100%';
        const source = document.createElement('kicanvas-source');
        source.setAttribute('src', src);
        embed.appendChild(source);
        el.replaceChildren(embed);
        detach = attachSelect(embed, (ref) => pick.current(ref));
        setError(null);
      },
      (e: unknown) => !cancelled && setError(e instanceof Error ? e.message : String(e)),
    );
    return () => {
      cancelled = true;
      detach();
    };
  }, [src, kTheme]);

  return (
    <div className="relative h-full w-full">
      <div ref={host} data-testid="kicanvas-host" className="h-full w-full" />
      {error && <p role="alert" className="absolute inset-x-0 top-2 text-center text-sm text-danger">{error}</p>}
    </div>
  );
}
```

`frontend/src/components/viewer/KicadPane.tsx` (go path):
```tsx
export { KicanvasPane as KicadPane } from './KicanvasPane';
```

`README.md` — add under a "Third-party software" heading: `KiCanvas (MIT) — vendored, unmodified except for removed Google Fonts loading, in frontend/public/vendor/kicanvas/ (see LICENSE.md and BUILD_INFO there).`

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && ./scripts/check-kicanvas.sh && npx vitest run src/lib/kicanvas.test.ts src/components/viewer/KicanvasPane.test.tsx && npm run typecheck && npm run lint && npm run build`
Expected: `PASS: kicanvas vendored (… bytes, commit …)`; vitest PASS (6 tests); build copies `public/vendor/kicanvas` into `dist/vendor/kicanvas`.

- [ ] **Step 5: Commit**

```bash
git add frontend/scripts frontend/public/vendor/kicanvas frontend/src/lib/kicanvas.ts frontend/src/lib/kicanvas.test.ts \
  frontend/src/components/viewer/types.ts frontend/src/components/viewer/KicanvasPane.tsx \
  frontend/src/components/viewer/KicanvasPane.test.tsx frontend/src/components/viewer/KicadPane.tsx \
  frontend/eslint.config.js README.md
git commit -m "feat(frontend): vendored KiCanvas pane with tap-to-mention

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6B.4: Agent-view pane and layer toggles

**Files:**
- Create: `frontend/src/components/viewer/AgentViewPane.tsx`, `frontend/src/components/viewer/LayerToggles.tsx`
- Test: `frontend/src/components/viewer/AgentViewPane.test.tsx`

**Interfaces:**
- Consumes: `agentViewUrl`, `PCB_LAYERS` (6B.1); `usePanZoom` (6B.2).
- Produces: `AgentViewPane({projectId, doc, version, theme, layers}: {projectId: string; doc: DocKind; version: number | null; theme: ResolvedTheme; layers: readonly PcbLayer[]})`; `LayerToggles({layers, onToggle}: {layers: readonly PcbLayer[]; onToggle(l: PcbLayer): void})`.

- [ ] **Step 1: Write the failing test**

`frontend/src/components/viewer/AgentViewPane.test.tsx`:
```tsx
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { AgentViewPane } from './AgentViewPane';
import { LayerToggles } from './LayerToggles';

describe('AgentViewPane', () => {
  it('requests the agent view with theme, layers and version', () => {
    render(<AgentViewPane projectId="p1" doc="pcb" version={4} theme="light" layers={['F.Cu', 'Edge.Cuts']} />);
    expect(screen.getByRole('img', { name: 'Agent view of the PCB' }).getAttribute('src'))
      .toBe('/api/projects/p1/agent-view?doc=pcb&theme=light&layers=F.Cu,Edge.Cuts&v=4');
  });

  it('shows an error when the render fails', () => {
    render(<AgentViewPane projectId="p1" doc="sch" version={null} theme="dark" layers={[]} />);
    fireEvent.error(screen.getByRole('img', { name: 'Agent view of the schematic' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Render failed');
  });
});

describe('LayerToggles', () => {
  it('renders eight checkboxes reflecting the selection and reports toggles', () => {
    const onToggle = vi.fn();
    render(<LayerToggles layers={['F.Cu']} onToggle={onToggle} />);
    expect(screen.getAllByRole('checkbox')).toHaveLength(8);
    expect(screen.getByRole('checkbox', { name: 'F.Cu' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'B.Cu' })).not.toBeChecked();
    fireEvent.click(screen.getByRole('checkbox', { name: 'User.Drawings' }));
    expect(onToggle).toHaveBeenCalledWith('User.Drawings');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/viewer/AgentViewPane.test.tsx`
Expected: FAIL — `Failed to resolve import "./AgentViewPane"`.

- [ ] **Step 3: Write minimal implementation**

`frontend/src/components/viewer/LayerToggles.tsx`:
```tsx
import { PCB_LAYERS, type PcbLayer } from '@/lib/viewUrls';

export function LayerToggles({ layers, onToggle }: { layers: readonly PcbLayer[]; onToggle: (l: PcbLayer) => void }) {
  return (
    <fieldset className="flex flex-wrap gap-x-3 gap-y-1 text-xs">
      <legend className="sr-only">Layers</legend>
      {PCB_LAYERS.map((l) => (
        <label key={l} className="flex items-center gap-1">
          <input type="checkbox" checked={layers.includes(l)} onChange={() => onToggle(l)} />
          {l}
        </label>
      ))}
    </fieldset>
  );
}
```

`frontend/src/components/viewer/AgentViewPane.tsx`:
```tsx
import { useEffect, useState } from 'react';
import { usePanZoom } from '@/hooks/usePanZoom';
import { agentViewUrl, type DocKind, type PcbLayer } from '@/lib/viewUrls';
import type { ResolvedTheme } from '@/stores/ui';

const LABEL: Record<DocKind, string> = { sch: 'schematic', pcb: 'PCB' };

export function AgentViewPane(p: {
  projectId: string; doc: DocKind; version: number | null; theme: ResolvedTheme; layers: readonly PcbLayer[];
}) {
  const url = agentViewUrl({ ...p });
  const { bind, style, reset } = usePanZoom();
  const [failed, setFailed] = useState(false);
  useEffect(() => { setFailed(false); reset(); }, [url, reset]);

  return (
    <div className="relative h-full w-full overflow-hidden bg-surface" {...bind}>
      <div style={style} className="h-full w-full">
        <img src={url} alt={`Agent view of the ${LABEL[p.doc]}`} draggable={false}
          onError={() => setFailed(true)} className="h-full w-full select-none object-contain" />
      </div>
      {failed && <p role="alert" className="absolute inset-x-0 top-2 text-center text-sm text-danger">Render failed</p>}
    </div>
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/components/viewer/AgentViewPane.test.tsx && npm run typecheck && npm run lint`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/viewer/AgentViewPane.tsx frontend/src/components/viewer/LayerToggles.tsx frontend/src/components/viewer/AgentViewPane.test.tsx
git commit -m "feat(frontend): agent-view pane with pan/zoom and PCB layer toggles

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6B.5: Viewer shell (tabs, view toggle, fullscreen) replacing ViewerSlot

**Files:**
- Create: `frontend/src/components/viewer/Viewer.tsx`
- Modify: `frontend/src/components/viewer/ViewerSlot.tsx`
- Test: `frontend/src/components/viewer/Viewer.test.tsx`

**Interfaces:**
- Consumes: `useView`, `ViewStoreContext`, `createViewStore` (6B.1); `KicadPane`, `insertMention` (6B.3 or 6B.F3); `AgentViewPane`, `LayerToggles` (6B.4); `useUi().resolvedTheme`.
- Produces: `Viewer({projectId}: {projectId: string})`; `ViewerSlot` keeps its 6A signature and renders `<Viewer projectId={projectId} />`.

Fullscreen is a CSS overlay (`fixed inset-0 z-50`), not the Fullscreen API (unsupported for elements on iPhone Safari). Pinch zoom inside fullscreen comes from KiCanvas (KiCad view) and `usePanZoom` (agent view, own viewer); the page itself is not zoomed (`touch-action: none` on the panes).

- [ ] **Step 1: Write the failing test**

`frontend/src/components/viewer/Viewer.test.tsx`:
```tsx
import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createViewStore, ViewStoreContext } from '@/stores/view';
import { FakeWsClient } from '@/test/fakeWsClient';
import type { KicadPaneProps } from './types';
import { Viewer } from './Viewer';

vi.mock('./KicadPane', () => ({
  KicadPane: (p: KicadPaneProps) => (
    <div data-testid="kicad-pane" data-doc={p.doc} data-version={String(p.version)} data-theme={p.theme} />
  ),
}));

let ws: FakeWsClient;

function renderViewer() {
  ws = new FakeWsClient();
  const store = createViewStore(ws);
  render(
    <ViewStoreContext.Provider value={store}>
      <Viewer projectId="p1" />
    </ViewStoreContext.Provider>,
  );
  return store;
}

const pane = () => screen.getByTestId('kicad-pane');
const agentImg = () => screen.getByRole('img', { name: /Agent view/ });

beforeEach(() => document.documentElement.setAttribute('data-theme', 'light'));

describe('Viewer', () => {
  it('switches between Schematic and PCB tabs', () => {
    renderViewer();
    expect(screen.getByRole('tab', { name: 'Schematic' })).toHaveAttribute('aria-selected', 'true');
    expect(pane().dataset.doc).toBe('sch');
    fireEvent.click(screen.getByRole('tab', { name: 'PCB' }));
    expect(screen.getByRole('tab', { name: 'PCB' })).toHaveAttribute('aria-selected', 'true');
    expect(pane().dataset.doc).toBe('pcb');
  });

  it('reloads the KiCad pane and the agent view on view-updated', () => {
    renderViewer();
    fireEvent.click(screen.getByRole('tab', { name: 'PCB' }));
    expect(pane().dataset.version).toBe('null');
    act(() => ws.emit({ type: 'view-updated', projectId: 'p1', docs: ['pcb'], version: 6 }));
    expect(pane().dataset.version).toBe('6');
    fireEvent.click(screen.getByRole('button', { name: 'Agent view' }));
    expect(agentImg().getAttribute('src')).toContain('&v=6');
    act(() => ws.emit({ type: 'view-updated', projectId: 'p1', docs: ['pcb'], version: 7 }));
    expect(agentImg().getAttribute('src')).toContain('&v=7');
  });

  it('shows layer toggles only for the PCB agent view and puts them in the URL', () => {
    renderViewer();
    fireEvent.click(screen.getByRole('button', { name: 'Agent view' }));
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
    fireEvent.click(screen.getByRole('tab', { name: 'PCB' }));
    expect(screen.getAllByRole('checkbox')).toHaveLength(8);
    fireEvent.click(screen.getByRole('checkbox', { name: 'B.Cu' }));
    expect(agentImg().getAttribute('src')).toContain('layers=F.Cu,F.SilkS,B.SilkS,Edge.Cuts');
    fireEvent.click(screen.getByRole('checkbox', { name: 'F.Fab' }));
    expect(agentImg().getAttribute('src')).toContain('layers=F.Cu,F.SilkS,B.SilkS,Edge.Cuts,F.Fab');
    fireEvent.click(screen.getByRole('button', { name: 'KiCad view' }));
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
    expect(pane()).toBeInTheDocument();
  });

  it('enters and leaves fullscreen', () => {
    renderViewer();
    const region = screen.getByRole('region', { name: 'Viewer' });
    expect(region.className).not.toContain('fixed');
    fireEvent.click(screen.getByRole('button', { name: 'Fullscreen' }));
    expect(screen.getByRole('region', { name: 'Viewer' }).className).toContain('fixed');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.getByRole('region', { name: 'Viewer' }).className).not.toContain('fixed');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/viewer/Viewer.test.tsx`
Expected: FAIL — `Failed to resolve import "./Viewer"`.

- [ ] **Step 3: Write minimal implementation**

`frontend/src/components/viewer/Viewer.tsx`:
```tsx
import { Maximize2, Minimize2 } from 'lucide-react';
import { useEffect } from 'react';
import type { DocKind } from '@/lib/viewUrls';
import { useUi } from '@/stores/ui';
import { useView } from '@/stores/view';
import { AgentViewPane } from './AgentViewPane';
import { KicadPane } from './KicadPane';
import { LayerToggles } from './LayerToggles';
import { insertMention } from './types';

const TABS: Array<{ doc: DocKind; label: string }> = [
  { doc: 'sch', label: 'Schematic' },
  { doc: 'pcb', label: 'PCB' },
];

const seg = (on: boolean) =>
  `rounded px-2 py-1 text-sm ${on ? 'bg-accent text-accent-fg' : 'text-muted hover:text-fg'}`;

export function Viewer({ projectId }: { projectId: string }) {
  const theme = useUi((s) => s.resolvedTheme);
  const v = useView((s) => s);
  const { setProject, setFullscreen, fullscreen } = v;

  useEffect(() => setProject(projectId), [projectId, setProject]);
  useEffect(() => {
    if (!fullscreen) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setFullscreen(false);
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [fullscreen, setFullscreen]);

  const version = v.versions[v.doc];
  return (
    <section aria-label="Viewer"
      className={`flex flex-col bg-bg ${fullscreen ? 'fixed inset-0 z-50 pb-[env(safe-area-inset-bottom)]' : 'h-full'}`}>
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-2 py-1">
        <div role="tablist" aria-label="Document" className="flex gap-1">
          {TABS.map((t) => (
            <button key={t.doc} role="tab" aria-selected={v.doc === t.doc} className={seg(v.doc === t.doc)}
              onClick={() => v.setDoc(t.doc)}>{t.label}</button>
          ))}
        </div>
        <div className="flex gap-1" aria-label="View mode">
          <button aria-pressed={v.mode === 'kicad'} className={seg(v.mode === 'kicad')} onClick={() => v.setMode('kicad')}>KiCad view</button>
          <button aria-pressed={v.mode === 'agent'} className={seg(v.mode === 'agent')} onClick={() => v.setMode('agent')}>Agent view</button>
        </div>
        <button className="ml-auto rounded p-1 text-muted hover:text-fg"
          aria-label={fullscreen ? 'Exit fullscreen' : 'Fullscreen'} onClick={() => setFullscreen(!fullscreen)}>
          {fullscreen ? <Minimize2 size={18} /> : <Maximize2 size={18} />}
        </button>
        {v.mode === 'agent' && v.doc === 'pcb' && (
          <div className="w-full"><LayerToggles layers={v.layers} onToggle={v.toggleLayer} /></div>
        )}
      </div>
      <div className="min-h-0 flex-1">
        {v.mode === 'kicad' ? (
          <KicadPane projectId={projectId} doc={v.doc} version={version} theme={theme} onPick={insertMention} />
        ) : (
          <AgentViewPane projectId={projectId} doc={v.doc} version={version} theme={theme} layers={v.layers} />
        )}
      </div>
    </section>
  );
}
```

`frontend/src/components/viewer/ViewerSlot.tsx` (replaces the 6A stub body; signature unchanged):
```tsx
import { Viewer } from './Viewer';

export function ViewerSlot({ projectId }: { projectId: string }) {
  return <Viewer projectId={projectId} />;
}
```

If a 6A test asserts the stub (`data-testid="viewer-slot"` / text "Viewer"), update that assertion in `frontend/src/layouts/ProjectLayout.test.tsx` to `screen.getByRole('region', { name: 'Viewer' })` and add `vi.mock('@/components/viewer/KicadPane', …)` as above so it does not load KiCanvas.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npm test -- --run && npm run typecheck && npm run lint && npm run build`
Expected: all tests PASS (Viewer: 4 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/viewer/Viewer.tsx frontend/src/components/viewer/Viewer.test.tsx frontend/src/components/viewer/ViewerSlot.tsx frontend/src/layouts/ProjectLayout.test.tsx
git commit -m "feat(frontend): viewer shell with doc tabs, view toggle and fullscreen

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6B.6: Manual check on devices (1B go only)

**Files:** none (results go into the PR description).

- [ ] **Step 1:** `cd frontend && npm run build`, start the backend with a fixture project (Phase 4 fixtures, `COPPERCHAT_PROJECTS_ROOT=backend/tests/fixtures/projects`), open it on desktop Chromium, iPad Safari and iPhone Safari.
- [ ] **Step 2:** Verify and tick in the PR body: schematic and PCB render; tapping a resistor inserts `@R… ` in the composer; after a turn that changes the PCB the KiCad view reloads; agent view toggles layers; fullscreen on iPhone pinch-zooms the agent view and KiCanvas; DevTools network tab shows no request to `fonts.googleapis.com`/`fonts.gstatic.com`.
- [ ] **Step 3:** Commit nothing; paste the checklist with results into the PR description.

---

### Task 6B.F1: Hit-test geometry (1B no-go only)

**Files:**
- Create: `backend/src/copperchat/views/hit_test.py`
- Test: `backend/tests/unit/test_hit_test.py`

**Interfaces:**
- Consumes: `Region` (C6, `copperchat.views`); `KicadSession.call`, `KicadHandle.board()`, `KicadHandle.schematic()` (C4); `DocKind`.
- Produces:
  - `@dataclass(frozen=True) class RefBox: ref: str; x_mm: float; y_mm: float; w_mm: float; h_mm: float` with `contains(x_mm, y_mm, tolerance_mm=0.0) -> bool`, `area -> float`
  - `pick_ref(boxes: Sequence[RefBox], x_mm: float, y_mm: float, *, tolerance_mm: float = 0.5) -> str | None` — smallest box containing the point (ties: alphabetically first ref)
  - `union_region(boxes: Sequence[RefBox], margin_mm: float = 5.0) -> Region | None`
  - `async collect_ref_boxes(session: KicadSession, doc: DocKind) -> list[RefBox]`

- [ ] **Step 1: Write the failing test**

`backend/tests/unit/test_hit_test.py`:
```python
from __future__ import annotations

import pytest

from copperchat.views import Region
from copperchat.views.hit_test import RefBox, pick_ref, union_region

BOXES = [
    RefBox("U1", 10.0, 10.0, 20.0, 20.0),
    RefBox("R12", 15.0, 15.0, 2.0, 1.0),   # inside U1's box: smaller wins
    RefBox("C3", 40.0, 10.0, 2.0, 1.0),
]


def test_contains_with_tolerance() -> None:
    b = RefBox("R1", 0.0, 0.0, 2.0, 1.0)
    assert b.contains(1.0, 0.5)
    assert not b.contains(2.4, 0.5)
    assert b.contains(2.4, 0.5, tolerance_mm=0.5)
    assert b.area == pytest.approx(2.0)


def test_pick_smallest_containing_box() -> None:
    assert pick_ref(BOXES, 16.0, 15.5) == "R12"
    assert pick_ref(BOXES, 25.0, 25.0) == "U1"
    assert pick_ref(BOXES, 41.0, 10.5) == "C3"


def test_pick_none_outside_everything() -> None:
    assert pick_ref(BOXES, 100.0, 100.0) is None
    assert pick_ref([], 0.0, 0.0) is None


def test_pick_uses_tolerance_for_tiny_parts() -> None:
    assert pick_ref(BOXES, 42.3, 10.5) == "C3"
    assert pick_ref(BOXES, 42.3, 10.5, tolerance_mm=0.0) is None


def test_ties_are_deterministic() -> None:
    same = [RefBox("R2", 0, 0, 1, 1), RefBox("R1", 0, 0, 1, 1)]
    assert pick_ref(same, 0.5, 0.5) == "R1"


def test_union_region_with_margin() -> None:
    assert union_region(BOXES, margin_mm=1.0) == Region(x_mm=9.0, y_mm=9.0, w_mm=34.0, h_mm=22.0)
    assert union_region([]) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_hit_test.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'copperchat.views.hit_test'`.

- [ ] **Step 3: Write minimal implementation**

`backend/src/copperchat/views/hit_test.py`:
```python
"""Map a point in mm to the component reference under it (own-viewer fallback, 1B no-go)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from kipy.proto.common.commands import editor_commands_pb2
from kipy.util.units import to_mm

from copperchat.kicad_session import DocKind, KicadHandle, KicadSession
from copperchat.views import Region


@dataclass(frozen=True)
class RefBox:
    ref: str
    x_mm: float
    y_mm: float
    w_mm: float
    h_mm: float

    def contains(self, x_mm: float, y_mm: float, tolerance_mm: float = 0.0) -> bool:
        t = tolerance_mm
        return (self.x_mm - t <= x_mm <= self.x_mm + self.w_mm + t
                and self.y_mm - t <= y_mm <= self.y_mm + self.h_mm + t)

    @property
    def area(self) -> float:
        return self.w_mm * self.h_mm


def pick_ref(boxes: Sequence[RefBox], x_mm: float, y_mm: float, *,
             tolerance_mm: float = 0.5) -> str | None:
    hits = [b for b in boxes if b.contains(x_mm, y_mm, tolerance_mm)]
    if not hits:
        return None
    return min(hits, key=lambda b: (b.area, b.ref)).ref


def union_region(boxes: Sequence[RefBox], margin_mm: float = 5.0) -> Region | None:
    if not boxes:
        return None
    x0 = min(b.x_mm for b in boxes) - margin_mm
    y0 = min(b.y_mm for b in boxes) - margin_mm
    x1 = max(b.x_mm + b.w_mm for b in boxes) + margin_mm
    y1 = max(b.y_mm + b.h_mm for b in boxes) + margin_mm
    return Region(x_mm=x0, y_mm=y0, w_mm=x1 - x0, h_mm=y1 - y0)


def _text(field: Any) -> str:
    """Reference text of a board Field or schematic SchematicField."""
    text = getattr(field, "text", None)
    value = getattr(text, "value", text)
    return value if isinstance(value, str) else str(getattr(field, "value", ""))


def _boxes(client: Any, document: Any, items: Sequence[Any]) -> dict[str, Any]:
    """GetBoundingBox for many items; returns item-id → Box2 proto (keeps ids aligned,
    unlike Board.get_item_bounding_box(list), which drops missing boxes)."""
    cmd = editor_commands_pb2.GetBoundingBox()
    cmd.header.document.CopyFrom(document)
    cmd.mode = editor_commands_pb2.BoundingBoxMode.BBM_ITEM_ONLY
    cmd.items.extend([i.id for i in items])
    resp = client.send(cmd, editor_commands_pb2.GetBoundingBoxResponse)
    return {kiid.value: box for kiid, box in zip(resp.items, resp.boxes, strict=False)}


def _collect(h: KicadHandle, doc: DocKind) -> list[RefBox]:
    if doc == "pcb":
        board = h.board()
        items = list(board.get_footprints())
        # kipy keeps the KiCadClient and DocumentSpecifier private on Board/Schematic.
        client, document = board._kicad, board._doc  # noqa: SLF001
    else:
        sch = h.schematic()
        items = list(sch.get_symbols())
        client, document = sch._kicad, sch._doc  # noqa: SLF001
    by_id = _boxes(client, document, items)
    out: list[RefBox] = []
    for item in items:
        box = by_id.get(item.id.value)
        ref = _text(item.reference_field)
        if box is None or not ref or ref.endswith("?") or ref.startswith("#"):
            continue  # unannotated parts and power symbols are not mentionable
        out.append(RefBox(ref, to_mm(box.position.x_nm), to_mm(box.position.y_nm),
                          to_mm(box.size.x_nm), to_mm(box.size.y_nm)))
    return out


async def collect_ref_boxes(session: KicadSession, doc: DocKind) -> list[RefBox]:
    return await session.call(lambda h: _collect(h, doc))
```
If `docs/spikes/1c-api-server.md` records a different call for schematic bounding boxes or a different private document attribute, use that.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/test_hit_test.py -q && uv run ruff check . && uv run mypy src`
Expected: 6 passed; ruff and mypy clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/views/hit_test.py backend/tests/unit/test_hit_test.py
git commit -m "feat(views): hit-test geometry for the own-viewer fallback

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6B.F2: Hit-test route and viewbox header (1B no-go only)

**Files:**
- Modify: `backend/src/copperchat/web/routes_views.py`, `backend/src/copperchat/views/hit_test.py` (add `svg_viewbox_mm`)
- Test: `backend/tests/unit/test_hit_test.py` (add), `backend/tests/kicad/test_hit_test_kicad.py`

**Interfaces:**
- Consumes: `collect_ref_boxes`, `pick_ref` (6B.F1); Phase 4's `agent-view` handler and its `mode=kicad` render, which (no-go path) renders via `kicad-cli sch|pcb export svg` + rasterisation.
- Produces:
  - `GET /api/projects/{project_id}/hit-test?doc=sch|pcb&x_mm=<float>&y_mm=<float>` → `{"ref": str | null}`; 409 `{"detail": "project not open"}` if `app.state.sessions.current()` is not this project
  - `GET /api/projects/{id}/agent-view?…&mode=kicad` adds header `X-CopperChat-Viewbox: <x_mm>,<y_mm>,<w_mm>,<h_mm>` (the mm rectangle the PNG covers) and `Access-Control-Expose-Headers` is not needed (same origin)
  - `svg_viewbox_mm(svg: str) -> tuple[float, float, float, float]`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/unit/test_hit_test.py`:
```python
from copperchat.views.hit_test import svg_viewbox_mm


def test_svg_viewbox_in_mm() -> None:
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="297.0022mm" height="210.0072mm" '
           'viewBox="0.0000 0.0000 297.0022 210.0072">')
    assert svg_viewbox_mm(svg) == pytest.approx((0.0, 0.0, 297.0022, 210.0072))


def test_svg_viewbox_scales_user_units_to_mm() -> None:
    svg = '<svg width="100mm" height="50mm" viewBox="10 20 1000 500">'
    assert svg_viewbox_mm(svg) == pytest.approx((1.0, 2.0, 100.0, 50.0))


def test_svg_viewbox_requires_mm_size() -> None:
    with pytest.raises(ValueError):
        svg_viewbox_mm('<svg width="100" height="50" viewBox="0 0 100 50">')
```

`backend/tests/kicad/test_hit_test_kicad.py` (Phase 4 provides session-level fixtures `ecc83_session` / `hier_session` and the helper `copy_fixture` in `tests/support/kicad_project.py`, but no app-level client fixture; this file defines `opened_client`, which yields an authenticated `httpx.AsyncClient` against a real app with the `ecc83-pp` fixture project opened, as `(client, project_id)`):
```python
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from copperchat.config import Settings
from copperchat.projects import discover_projects
from copperchat.views.hit_test import collect_ref_boxes
from copperchat.web.app import create_app
from tests.support.kicad_project import copy_fixture

pytestmark = pytest.mark.kicad

GOOD = {"username": "admin", "password": "test-password"}


@pytest.fixture
async def opened_client(
    make_settings: Callable[..., Settings],
) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    settings = make_settings(kicad_cli="kicad-cli", kicad_api_timeout_s=30.0, kicad_start_timeout_s=60.0)
    copy_fixture("ecc83", settings.projects_root)
    project = next(p for p in discover_projects(settings.projects_root) if p.name == "ecc83-pp")
    app = create_app(settings)   # real KicadCliLauncher
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post("/api/auth/login", json=GOOD)).status_code == 204
            assert (await client.post(f"/api/projects/{project.id}/open")).status_code == 200
            yield client, project.id
        await app.state.sessions.close_all()


async def test_hit_test_finds_a_real_footprint(opened_client: Any) -> None:
    client, project_id = opened_client
    session = client._transport.app.state.sessions.current()  # noqa: SLF001
    boxes = await collect_ref_boxes(session, "pcb")
    assert boxes, "fixture board has footprints"
    target = min(boxes, key=lambda b: b.area)
    cx, cy = target.x_mm + target.w_mm / 2, target.y_mm + target.h_mm / 2
    r = await client.get(f"/api/projects/{project_id}/hit-test",
                         params={"doc": "pcb", "x_mm": cx, "y_mm": cy})
    assert r.status_code == 200
    assert r.json() == {"ref": target.ref}
    far = await client.get(f"/api/projects/{project_id}/hit-test",
                           params={"doc": "pcb", "x_mm": -1000, "y_mm": -1000})
    assert far.json() == {"ref": None}


async def test_schematic_symbols_have_boxes(opened_client: Any) -> None:
    client, _ = opened_client
    session = client._transport.app.state.sessions.current()  # noqa: SLF001
    refs = {b.ref for b in await collect_ref_boxes(session, "sch")}
    assert any(r.startswith("U") for r in refs)
    assert not any(r.startswith("#") for r in refs)


async def test_kicad_mode_render_reports_viewbox_covering_footprints(opened_client: Any) -> None:
    client, project_id = opened_client
    r = await client.get(f"/api/projects/{project_id}/agent-view",
                         params={"doc": "pcb", "theme": "light", "mode": "kicad"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    x, y, w, h = (float(v) for v in r.headers["X-CopperChat-Viewbox"].split(","))
    session = client._transport.app.state.sessions.current()  # noqa: SLF001
    for b in await collect_ref_boxes(session, "pcb"):
        assert x <= b.x_mm and b.x_mm + b.w_mm <= x + w
        assert y <= b.y_mm and b.y_mm + b.h_mm <= y + h


async def test_hit_test_rejects_bad_doc(opened_client: Any) -> None:
    client, project_id = opened_client
    r = await client.get(f"/api/projects/{project_id}/hit-test",
                         params={"doc": "gerber", "x_mm": 0, "y_mm": 0})
    assert r.status_code == 422
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_hit_test.py -q && uv run pytest -m kicad tests/kicad/test_hit_test_kicad.py -q`
Expected: FAIL — `ImportError: cannot import name 'svg_viewbox_mm'`; kicad tests FAIL with 404 on `/hit-test` and `KeyError: 'x-copperchat-viewbox'`.

- [ ] **Step 3: Write minimal implementation**

Append to `backend/src/copperchat/views/hit_test.py`:
```python
import re

_SVG_ATTR = re.compile(r'\b(width|height|viewBox)="([^"]+)"')


def svg_viewbox_mm(svg: str) -> tuple[float, float, float, float]:
    """mm rectangle covered by a kicad-cli SVG (root width/height in mm + viewBox)."""
    head = svg[: svg.find(">", svg.find("<svg")) + 1]
    attrs = dict(_SVG_ATTR.findall(head))
    if not attrs.get("width", "").endswith("mm") or not attrs.get("height", "").endswith("mm"):
        raise ValueError("SVG root has no mm width/height")
    w_mm, h_mm = float(attrs["width"][:-2]), float(attrs["height"][:-2])
    vx, vy, vw, vh = (float(v) for v in attrs["viewBox"].replace(",", " ").split())
    sx, sy = w_mm / vw, h_mm / vh
    return (vx * sx, vy * sy, w_mm, h_mm)
```
(Move the `import re` to the module's import block.)

`backend/src/copperchat/web/routes_views.py` — add the route (same `router`, prefix `/api/projects`, as Phase 4) and the header in the `agent-view` handler:
```python
from typing import Literal

from fastapi import Depends, HTTPException, Query, Request

from copperchat.views.hit_test import collect_ref_boxes, pick_ref


def _open_session(request: Request, project_id: str) -> KicadSession:
    session = request.app.state.sessions.current()
    if session is None or session.project.id != project_id:
        raise HTTPException(status_code=409, detail="project not open")
    return session


@router.get("/{project_id}/hit-test")
async def hit_test(project_id: str, request: Request,
                   doc: Literal["sch", "pcb"] = Query(...),
                   x_mm: float = Query(...), y_mm: float = Query(...),
                   _: User = Depends(require_user)) -> dict[str, str | None]:
    session = _open_session(request, project_id)
    boxes = await collect_ref_boxes(session, doc)
    return {"ref": pick_ref(boxes, x_mm, y_mm)}
```
In the existing `agent-view` handler, for `mode == "kicad"`: the Phase 4 kicad-mode renderer exports SVG via `kicad-cli` before rasterising; read that SVG text, compute `vb = svg_viewbox_mm(svg_text)` and return
```python
return Response(content=png, media_type="image/png",
                headers={"X-CopperChat-Viewbox": ",".join(f"{v:.4f}" for v in vb),
                         "Cache-Control": "no-cache"})
```
To get the SVG text out of the renderer without changing C6's `render_view` signature, add the optional field `viewbox_mm: tuple[float, float, float, float] | None = None` to Phase 4's `ImageBlock` and set it in the kicad-mode renderer (`viewbox_mm=svg_viewbox_mm(svg_text)`); the route emits the header when it is not `None`. (Report this `ImageBlock` addition to the coordinator.) If `docs/spikes/1c-api-server.md` records a PNG export with a known pixel→mm mapping instead, compute the viewbox from that mapping.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest -m "not kicad" -q && uv run pytest -m kicad tests/kicad/test_hit_test_kicad.py -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src`
Expected: unit tests pass (9 in `test_hit_test.py`); 4 kicad tests pass; lint/type clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/views backend/src/copperchat/web/routes_views.py backend/tests/unit/test_hit_test.py backend/tests/kicad/test_hit_test_kicad.py
git commit -m "feat(api): hit-test endpoint and kicad-mode viewbox header

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6B.F3: OwnViewerPane as KicadPane (1B no-go only)

**Files:**
- Create: `frontend/src/components/viewer/types.ts` (identical to the 6B.3 version), `frontend/src/components/viewer/OwnViewerPane.tsx`, `frontend/src/components/viewer/KicadPane.tsx`
- Test: `frontend/src/components/viewer/OwnViewerPane.test.tsx`

**Interfaces:**
- Consumes: `apiGet` from `@/api/http`; `agentViewUrl(..., mode: 'kicad')` (6B.1); `usePanZoom`, `toContent` (6B.2); `GET /hit-test`, `X-CopperChat-Viewbox` (6B.F2).
- Produces: `KicadPaneProps`, `insertMention` (same as 6B.3); `OwnViewerPane(p: KicadPaneProps)`; `KicadPane` re-export; `parseViewbox(h: string | null): Viewbox | null`; `clickToMm(o: {px: Pt; view: PanZoomView; box: {w: number; h: number}; natural: {w: number; h: number}; viewbox: Viewbox}): Pt | null` (accounts for `object-contain` letterboxing).

The pane fetches the PNG itself (to read the header), shows it via an object URL, and maps a tap (not a drag) to mm.

- [ ] **Step 1: Write the failing test**

`frontend/src/components/viewer/OwnViewerPane.test.tsx`:
```tsx
import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { IDENTITY } from '@/lib/panZoom';
import { useComposer } from '@/stores/composer';
import { clickToMm, OwnViewerPane, parseViewbox } from './OwnViewerPane';
import { insertMention } from './types';

describe('viewbox mapping', () => {
  it('parses the header', () => {
    expect(parseViewbox('0,0,297,210')).toEqual({ x: 0, y: 0, w: 297, h: 210 });
    expect(parseViewbox('nope')).toBeNull();
    expect(parseViewbox(null)).toBeNull();
  });
  it('maps a click to mm with letterboxing', () => {
    // 400x400 box, 200x100 image → drawn 400x200, offset y=100
    const o = { view: IDENTITY, box: { w: 400, h: 400 }, natural: { w: 200, h: 100 }, viewbox: { x: 10, y: 20, w: 100, h: 50 } };
    expect(clickToMm({ ...o, px: { x: 200, y: 200 } })).toEqual({ x: 60, y: 45 });
    expect(clickToMm({ ...o, px: { x: 200, y: 50 } })).toBeNull();   // in the letterbox
  });
  it('undoes pan/zoom', () => {
    const o = { box: { w: 100, h: 100 }, natural: { w: 100, h: 100 }, viewbox: { x: 0, y: 0, w: 100, h: 100 } };
    expect(clickToMm({ ...o, view: { scale: 2, x: -50, y: -50 }, px: { x: 50, y: 50 } })).toEqual({ x: 50, y: 50 });
  });
});

describe('OwnViewerPane', () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    useComposer.getState().clear();
    vi.stubGlobal('fetch', fetchMock);
    URL.createObjectURL = vi.fn(() => 'blob:png');
    URL.revokeObjectURL = vi.fn();
  });
  afterEach(() => { vi.unstubAllGlobals(); fetchMock.mockReset(); });

  it('inserts @R12 for a tap on R12', async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url.includes('/agent-view')) {
        return new Response(new Blob(['png']), { status: 200, headers: { 'X-CopperChat-Viewbox': '0,0,100,100' } });
      }
      expect(url).toBe('/api/projects/p1/hit-test?doc=pcb&x_mm=25&y_mm=50');
      return new Response(JSON.stringify({ ref: 'R12' }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    });
    render(<OwnViewerPane projectId="p1" doc="pcb" version={3} theme="light" onPick={insertMention} />);
    const img = await screen.findByRole('img', { name: 'PCB' });
    expect(fetchMock.mock.calls[0][0]).toBe('/api/projects/p1/agent-view?doc=pcb&theme=light&mode=kicad&v=3');
    Object.defineProperty(img, 'naturalWidth', { value: 100 });
    Object.defineProperty(img, 'naturalHeight', { value: 100 });
    const box = img.parentElement!.parentElement!;
    box.getBoundingClientRect = () => ({ left: 0, top: 0, width: 200, height: 200, right: 200, bottom: 200, x: 0, y: 0, toJSON: () => ({}) });
    await act(async () => { fireEvent.click(box, { clientX: 50, clientY: 100 }); });
    expect(useComposer.getState().text).toBe('@R12 ');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/viewer/OwnViewerPane.test.tsx`
Expected: FAIL — `Failed to resolve import "./OwnViewerPane"`.

- [ ] **Step 3: Write minimal implementation**

`frontend/src/components/viewer/types.ts`: exactly the file from Task 6B.3 Step 3 (`KicadPaneProps`, `insertMention`).

`frontend/src/components/viewer/OwnViewerPane.tsx`:
```tsx
import { type MouseEvent, useEffect, useRef, useState } from 'react';
import { apiGet } from '@/api/http';
import { usePanZoom } from '@/hooks/usePanZoom';
import { type PanZoomView, type Pt, toContent } from '@/lib/panZoom';
import { agentViewUrl } from '@/lib/viewUrls';
import type { KicadPaneProps } from './types';

export interface Viewbox { x: number; y: number; w: number; h: number }

export function parseViewbox(h: string | null): Viewbox | null {
  const n = (h ?? '').split(',').map(Number);
  if (n.length !== 4 || n.some((v) => !Number.isFinite(v)) || n[2] <= 0 || n[3] <= 0) return null;
  return { x: n[0], y: n[1], w: n[2], h: n[3] };
}

export function clickToMm(o: {
  px: Pt; view: PanZoomView; box: { w: number; h: number }; natural: { w: number; h: number }; viewbox: Viewbox;
}): Pt | null {
  const c = toContent(o.view, o.px.x, o.px.y);
  const s = Math.min(o.box.w / o.natural.w, o.box.h / o.natural.h);   // object-contain
  const dw = o.natural.w * s;
  const dh = o.natural.h * s;
  const ix = c.x - (o.box.w - dw) / 2;
  const iy = c.y - (o.box.h - dh) / 2;
  if (ix < 0 || iy < 0 || ix > dw || iy > dh) return null;
  return { x: o.viewbox.x + (ix / dw) * o.viewbox.w, y: o.viewbox.y + (iy / dh) * o.viewbox.h };
}

const round = (v: number) => Math.round(v * 1000) / 1000;

export function OwnViewerPane({ projectId, doc, version, theme, onPick }: KicadPaneProps) {
  const url = agentViewUrl({ projectId, doc, theme, layers: [], version, mode: 'kicad' });
  const [img, setImg] = useState<{ src: string; viewbox: Viewbox | null } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const { bind, style, view, reset, moved } = usePanZoom();

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    reset();
    void fetch(url, { credentials: 'same-origin' }).then(async (r) => {
      if (!r.ok) throw new Error(`Render failed (${r.status})`);
      const viewbox = parseViewbox(r.headers.get('X-CopperChat-Viewbox'));
      objectUrl = URL.createObjectURL(await r.blob());
      if (!cancelled) { setImg({ src: objectUrl, viewbox }); setError(null); }
    }).catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [url, reset]);

  async function onClick(e: MouseEvent<HTMLDivElement>) {
    const el = imgRef.current;
    if (moved() || !el || !img?.viewbox || !el.naturalWidth) return;
    const r = e.currentTarget.getBoundingClientRect();
    const mm = clickToMm({
      px: { x: e.clientX - r.left, y: e.clientY - r.top }, view,
      box: { w: r.width, h: r.height }, natural: { w: el.naturalWidth, h: el.naturalHeight }, viewbox: img.viewbox,
    });
    if (!mm) return;
    const q = `doc=${doc}&x_mm=${round(mm.x)}&y_mm=${round(mm.y)}`;
    const res = await apiGet<{ ref: string | null }>(`/projects/${encodeURIComponent(projectId)}/hit-test?${q}`);
    if (res.ref) onPick(res.ref);
  }

  return (
    <div className="relative h-full w-full overflow-hidden bg-surface" {...bind} onClick={(e) => void onClick(e)}>
      <div style={style} className="h-full w-full">
        {img && <img ref={imgRef} src={img.src} alt={doc === 'pcb' ? 'PCB' : 'Schematic'} draggable={false}
          className="h-full w-full select-none object-contain" />}
      </div>
      {error && <p role="alert" className="absolute inset-x-0 top-2 text-center text-sm text-danger">{error}</p>}
    </div>
  );
}
```
(`apiGet<T>(path)` is the 6A helper: prefixes `/api`, sends cookies, parses JSON, throws `ApiError`.)

`frontend/src/components/viewer/KicadPane.tsx` (no-go path):
```tsx
export { OwnViewerPane as KicadPane } from './OwnViewerPane';
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npm test -- --run && npm run typecheck && npm run lint && npm run build`
Expected: all PASS (OwnViewerPane: 4 tests; Viewer tests from 6B.5 unchanged because they mock `./KicadPane`).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/viewer/types.ts frontend/src/components/viewer/OwnViewerPane.tsx \
  frontend/src/components/viewer/OwnViewerPane.test.tsx frontend/src/components/viewer/KicadPane.tsx
git commit -m "feat(frontend): own-viewer pane with hit-test tap-to-mention

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Task order on the no-go path: 6B.1, 6B.2, 6B.F1, 6B.F2, 6B.F3, 6B.4, 6B.5 (6B.5 needs `KicadPane`). On the go path: 6B.1–6B.6 in order.

---

## Phase self-review

- **Spec coverage:** §4.2 viewer (schematic/PCB tabs, KiCanvas, agent-view toggle, layer toggles, tap → `@REF`, fallback own viewer) — 6B.3–6B.5, 6B.F1–F3; §4.2 layouts (iPhone fullscreen pinch-zoom; GUI agent view follows the theme via `theme=resolvedTheme`) — 6B.2, 6B.4, 6B.5; §5.1 `view-updated` handling — 6B.1; §11 testing (no KiCad mocks for the KiCad-reading hit-test: `@pytest.mark.kicad` on fixture projects) — 6B.F2; §12 licensing (KiCanvas MIT vendored with LICENSE, no third-party font requests, no "Claude Code" wording) — 6B.3.
- **Index gate:** 1B go/no-go selects 6B.3+6B.6 vs. 6B.F1–F3; both export `KicadPane(KicadPaneProps)`.
- **Deviations / new cross-phase names (for the index):**
  - Vendored KiCanvas lives in `frontend/public/vendor/kicanvas/` (as spike 1B's follow-up names it, loaded by `<script type="module">`), not in `frontend/vendor/kicanvas/`, so the bundle is served as-is and not linted/bundled by Vite.
  - Frontend: `@/stores/view` (`createViewStore`, `viewStore`, `ViewStoreContext`, `useView`), `@/lib/viewUrls` (`snapshotUrl`, `agentViewUrl`, `PCB_LAYERS`, `DEFAULT_LAYERS`), `@/lib/panZoom`, `@/hooks/usePanZoom`, `@/components/viewer/types` (`KicadPaneProps`, `insertMention`), `@/components/viewer/KicadPane`.
  - Phase 4 contract assumptions: `GET /view/{doc}` and `GET /agent-view` send `Cache-Control: no-cache` (URLs without `v` before the first `view-updated`); `agent-view` accepts `mode=agent|kicad` and ignores the `v` cache-buster.
  - No-go only: `GET /api/projects/{id}/hit-test` → `{ref}`; header `X-CopperChat-Viewbox`; `ImageBlock.viewbox_mm` optional field; `copperchat.views.hit_test.{RefBox, pick_ref, union_region, collect_ref_boxes, svg_viewbox_mm}`; kicad-test fixture `opened_client` → `(client, project_id)` defined locally in `test_hit_test_kicad.py`.
- **Open points:** KiCanvas theme mapping (`kicad` is itself a dark theme; if 1B finds no light theme, light mode keeps `kicad`); schematic `GetBoundingBox` support at the pinned KiCad commit is confirmed by the 6B.F2 kicad test only.
