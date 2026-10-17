import { beforeEach, describe, expect, it } from 'vitest';
import { FakeWsClient } from '@/test/fakeWsClient';
import { createViewStore } from './view';

const P1 = 'a1b2c3d4e5f6';
const P2 = 'b1b2c3d4e5f6';
let ws: FakeWsClient;
let store: ReturnType<typeof createViewStore>;

const updated = (docs: Array<'sch' | 'pcb'>, version: number, projectId = P1) =>
  ws.emit({ type: 'view-updated', projectId, docs, version });

beforeEach(() => {
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
  it('stops listening after dispose', () => {
    store.getState().dispose();
    updated(['pcb'], 3);
    expect(store.getState().versions.pcb).toBeNull();
  });
});
