import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { FakeWsClient } from '@/test/fakeWsClient';
import { createViewStore } from './view';
import { LIVE_VIEW_KEY } from './viewerPrefs';

const P1 = 'a1b2c3d4e5f6';
const P2 = 'b1b2c3d4e5f6';
let ws: FakeWsClient;
let store: ReturnType<typeof createViewStore>;

const updated = (docs: Array<'sch' | 'pcb'>, version: number, projectId = P1) =>
  ws.emit({ type: 'view-updated', projectId, docs, version });

beforeEach(() => {
  localStorage.clear();
  ws = new FakeWsClient();
  store = createViewStore(ws);
  store.getState().setProject(P1);
});

describe('view store', () => {
  it('tracks the version and bumps the reload counter per doc on view-updated', () => {
    updated(['pcb'], 3);
    expect(store.getState().versions).toEqual({ sch: null, pcb: 3 });
    expect(store.getState().revs).toEqual({ sch: null, pcb: 1 });
    updated(['sch', 'pcb'], 5);
    expect(store.getState().versions).toEqual({ sch: 5, pcb: 5 });
    expect(store.getState().revs).toEqual({ sch: 1, pcb: 2 });
  });
  it('ignores replayed or older frames', () => {
    updated(['pcb'], 5);
    updated(['pcb'], 5);
    updated(['pcb'], 4);
    expect(store.getState().versions.pcb).toBe(5);
    expect(store.getState().revs.pcb).toBe(1);
  });
  it('accepts a lower version after a resumed connection (server counter restarted, X2)', () => {
    updated(['sch', 'pcb'], 5);
    ws.resume();
    updated(['pcb'], 1);
    expect(store.getState().versions.pcb).toBe(1);
    expect(store.getState().revs.pcb).toBe(2);
    updated(['pcb'], 1); // only the first frame after the reconnect may go backwards
    expect(store.getState().revs.pcb).toBe(2);
    updated(['sch'], 0); // sch still had its pending reset
    expect(store.getState().versions.sch).toBe(0);
  });
  it('accepts a restarted counter after a plain reconnect too (backend restart reports open)', () => {
    updated(['pcb'], 5);
    ws.drop();
    ws.connect();
    updated(['pcb'], 0);
    expect(store.getState().versions.pcb).toBe(0);
    expect(store.getState().revs.pcb).toBe(2);
  });
  it('ignores other projects', () => {
    updated(['pcb'], 9, P2);
    expect(store.getState().versions.pcb).toBeNull();
  });
  it('resets versions and fullscreen on every project open', () => {
    updated(['pcb'], 2);
    store.getState().setFullscreen(true);
    store.getState().setProject(P2);
    expect(store.getState().versions).toEqual({ sch: null, pcb: null });
    expect(store.getState().revs).toEqual({ sch: null, pcb: null });
    expect(store.getState().kicadRevs).toEqual({ sch: null, pcb: null });
    expect(store.getState().fullscreen).toBe(false);
    store.getState().setProject(P2); // re-open of the same project (new session) also resets
    updated(['pcb'], 0, P2);
    expect(store.getState().versions.pcb).toBe(0);
  });
  it('toggles layers keeping canonical order', () => {
    store.getState().toggleLayer('F.Cu');
    expect(store.getState().layers).toEqual(['B.Cu', 'F.SilkS', 'B.SilkS', 'Edge.Cuts']);
    store.getState().toggleLayer('B.Fab');
    store.getState().toggleLayer('F.Cu');
    expect(store.getState().layers).toEqual(['F.Cu', 'B.Cu', 'F.SilkS', 'B.SilkS', 'Edge.Cuts', 'B.Fab']);
  });
  it('setHighlight bumps seq, switches to the KiCad schematic view; clearHighlight drops it', () => {
    store.getState().setDoc('pcb');
    store.getState().setMode('agent');
    store.getState().setHighlight({ refs: ['R1'], boxes: [], sheetPath: '/root' });
    const h1 = store.getState().highlight!;
    expect(h1).toMatchObject({ refs: ['R1'], boxes: [], sheetPath: '/root' });
    expect(store.getState().doc).toBe('sch');
    expect(store.getState().mode).toBe('kicad');
    store.getState().setHighlight({ refs: ['R1'], boxes: [], sheetPath: '/root' });
    expect(store.getState().highlight!.seq).toBeGreaterThan(h1.seq);
    store.getState().clearHighlight();
    expect(store.getState().highlight).toBeNull();
  });
  it('setFind bumps seq per request (same ref twice is distinct); null clears', () => {
    store.getState().setFind('U1');
    const f1 = store.getState().find!;
    store.getState().setFind('U1');
    expect(store.getState().find).toEqual({ ref: 'U1', seq: f1.seq + 1 });
    store.getState().setFind(null);
    expect(store.getState().find).toBeNull();
  });
  it('every project open bumps openSeq (revs restart then)', () => {
    const before = store.getState().openSeq;
    store.getState().setProject(P1);
    expect(store.getState().openSeq).toBe(before + 1);
  });
  it('a project switch clears highlight and find', () => {
    store.getState().setHighlight({ refs: ['R1'], boxes: [], sheetPath: null });
    store.getState().setFind('U1');
    store.getState().setProject(P2);
    expect(store.getState().highlight).toBeNull();
    expect(store.getState().find).toBeNull();
  });
  it('stops listening after dispose', () => {
    store.getState().dispose();
    updated(['pcb'], 3);
    expect(store.getState().versions.pcb).toBeNull();
  });
});

describe('live schematic view', () => {
  const sch = (version: number, changedFiles?: string[], live = false) =>
    ws.emit({ type: 'view-updated', projectId: P1, docs: ['sch'], version, ...(changedFiles ? { changedFiles } : {}), ...(live ? { live: true } : {}) });
  const st = () => store.getState();

  beforeEach(() => st().setActiveSheet('top.kicad_sch'));

  it('is on by default', () => {
    expect(st().live).toBe(true);
  });

  it('a live frame touching the shown sheet reloads the KiCad pane', () => {
    sch(1, ['top.kicad_sch'], true);
    expect(st().revs.sch).toBeNull(); // agent view and Changes panel wait for the turn end
    expect(st().kicadRevs.sch).toBe(1);
    expect(st().staleSheets).toEqual([]);
  });

  it('live frames never bump revs; the turn-end frame does (agent view, Changes panel)', () => {
    sch(1, ['top.kicad_sch'], true);
    sch(2, ['sub/power.kicad_sch'], true);
    expect(st().revs.sch).toBeNull();
    sch(3, []);
    expect(st().revs.sch).toBe(1);
    expect(st().kicadRevs.sch).toBe(1);
  });

  it('a live frame touching only another sheet marks it stale without a reload', () => {
    sch(1, ['sub/power.kicad_sch'], true);
    expect(st().revs.sch).toBeNull();
    expect(st().kicadRevs.sch).toBeNull();
    expect(st().staleSheets).toEqual(['sub/power.kicad_sch']);
    sch(2, ['sub/power.kicad_sch', 'sub/reg.kicad_sch'], true);
    expect(st().staleSheets).toEqual(['sub/power.kicad_sch', 'sub/reg.kicad_sch']);
    expect(st().kicadRevs.sch).toBeNull();
  });

  it('switching to a stale sheet reloads and clears the stale marks', () => {
    sch(1, ['sub/power.kicad_sch', 'sub/reg.kicad_sch'], true);
    st().setActiveSheet('top.kicad_sch'); // not stale: nothing
    expect(st().kicadRevs.sch).toBeNull();
    st().setActiveSheet('power.kicad_sch'); // KiCanvas may only know the basename
    expect(st().activeSheet).toBe('power.kicad_sch');
    expect(st().kicadRevs.sch).toBe(1);
    expect(st().staleSheets).toEqual([]);
  });

  it('a reload clears every stale mark (all files load fresh)', () => {
    sch(1, ['sub/power.kicad_sch'], true);
    sch(2, ['top.kicad_sch'], true);
    expect(st().kicadRevs.sch).toBe(1);
    expect(st().staleSheets).toEqual([]);
  });

  it('clearStale drops only the sheets known when the full load started', () => {
    sch(1, ['sub/power.kicad_sch'], true);
    const atLoadStart = [...st().staleSheets];
    sch(2, ['sub/reg.kicad_sch'], true); // arrives while the pane loads
    st().clearStale(atLoadStart);
    expect(st().staleSheets).toEqual(['sub/reg.kicad_sch']);
    const before = st().staleSheets;
    st().clearStale(['sub/other.kicad_sch']);
    expect(st().staleSheets).toBe(before); // nothing to drop: no new state
  });

  it('matches by basename', () => {
    st().setActiveSheet('power.kicad_sch');
    sch(1, ['sub/power.kicad_sch'], true);
    expect(st().kicadRevs.sch).toBe(1);
  });

  it('reloads when changedFiles is absent (unknown) or no sheet is known yet', () => {
    sch(1);
    expect(st().kicadRevs.sch).toBe(1);
    st().setActiveSheet(null);
    sch(2, ['sub/power.kicad_sch'], true);
    expect(st().kicadRevs.sch).toBe(2);
  });

  it('empty changes do not reload the pane', () => {
    sch(1, []);
    expect(st().revs.sch).toBe(1);
    expect(st().kicadRevs.sch).toBeNull();
    expect(st().staleSheets).toEqual([]);
  });

  it('live off defers live frames; the turn-end frame applies them with its own changes', () => {
    st().setLive(false);
    sch(1, ['sub/power.kicad_sch'], true);
    sch(2, ['top.kicad_sch'], true);
    expect(st().versions.sch).toBe(2);
    expect(st().revs.sch).toBeNull();
    expect(st().kicadRevs.sch).toBeNull();
    sch(2, ['top.kicad_sch'], true); // replay still rejected
    sch(3, []);
    expect(st().revs.sch).toBe(1);
    expect(st().kicadRevs.sch).toBe(1);
    sch(4, ['sub/power.kicad_sch'], true);
    sch(5, []);
    expect(st().kicadRevs.sch).toBe(1);
    expect(st().staleSheets).toEqual(['sub/power.kicad_sch']);
  });

  it('live off: a turn-end frame touching only other sheets marks them stale', () => {
    st().setLive(false);
    sch(1, ['sub/reg.kicad_sch'], true);
    sch(2, ['sub/power.kicad_sch']);
    expect(st().revs.sch).toBe(1);
    expect(st().kicadRevs.sch).toBeNull();
    expect(st().staleSheets).toEqual(['sub/reg.kicad_sch', 'sub/power.kicad_sch']);
  });

  it('turning live back on applies the deferred changes', () => {
    st().setLive(false);
    sch(1, ['top.kicad_sch'], true);
    st().setLive(true);
    expect(st().revs.sch).toBeNull(); // still live changes: the turn-end frame bumps it
    expect(st().kicadRevs.sch).toBe(1);
    st().setLive(true); // nothing deferred any more
    expect(st().kicadRevs.sch).toBe(1);
  });

  it('pcb frames reload the board pane as before', () => {
    ws.emit({ type: 'view-updated', projectId: P1, docs: ['pcb'], version: 1, changedFiles: [] });
    ws.emit({ type: 'view-updated', projectId: P1, docs: ['sch', 'pcb'], version: 2, changedFiles: ['sub/power.kicad_sch'] });
    expect(st().kicadRevs.pcb).toBe(2);
    expect(st().revs.pcb).toBe(2);
    expect(st().kicadRevs.sch).toBeNull();
  });

  it('the first frame after a reconnect counts as "everything changed" (frames may be lost)', () => {
    sch(1, ['sub/power.kicad_sch'], true);
    ws.resume();
    sch(0, ['sub/power.kicad_sch'], true);
    expect(st().kicadRevs.sch).toBe(1);
    sch(1, ['sub/power.kicad_sch'], true);
    expect(st().kicadRevs.sch).toBe(1);
    expect(st().staleSheets).toEqual(['sub/power.kicad_sch']);
  });

  it('a deferred frame after a reconnect also counts as everything', () => {
    st().setLive(false);
    ws.drop();
    ws.connect();
    sch(0, ['sub/power.kicad_sch'], true);
    sch(1, []);
    expect(st().kicadRevs.sch).toBe(1);
  });

  it('a project open resets the sheet state and deferred changes', () => {
    st().setLive(false);
    sch(1, ['top.kicad_sch'], true);
    st().setProject(P1);
    expect(st()).toMatchObject({ activeSheet: null, staleSheets: [], kicadRevs: { sch: null, pcb: null } });
    st().setLive(true);
    expect(st().revs.sch).toBeNull();
  });

  describe('pref', () => {
    afterEach(() => vi.restoreAllMocks());

    it('persists the toggle per browser', () => {
      st().setLive(false);
      expect(localStorage.getItem(LIVE_VIEW_KEY)).toBe('false');
      expect(createViewStore(new FakeWsClient()).getState().live).toBe(false);
      st().setLive(true);
      expect(createViewStore(new FakeWsClient()).getState().live).toBe(true);
    });

    it('survives unavailable storage (default on, toggle applies for the session)', () => {
      vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
        throw new Error('denied');
      });
      vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
        throw new Error('denied');
      });
      const s = createViewStore(new FakeWsClient());
      expect(s.getState().live).toBe(true);
      s.getState().setLive(false);
      expect(s.getState().live).toBe(false);
    });
  });
});
