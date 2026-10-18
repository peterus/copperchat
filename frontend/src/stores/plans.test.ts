import { describe, expect, it, vi } from 'vitest';
import type { PlanDTO } from '@/protocol/frames';
import { FakeWsClient } from '@/test/fakeWsClient';
import { createPlansStore, type PlansDeps } from './plans';

const PID = 'a1b2c3d4e5f6';
const OTHER = 'ffffffffffff';
const plan = (over: Partial<PlanDTO> = {}): PlanDTO => ({
  id: 'p'.repeat(32), projectId: PID, title: 'T', summary: '', status: 'proposed', revision: 1, pauseReason: null,
  steps: [], createdAt: '2026-10-18T10:00:00.000Z', updatedAt: '2026-10-18T10:00:00.000Z', ...over,
});
const deps = (over: Partial<PlansDeps> = {}): PlansDeps => ({
  fetchOpen: vi.fn(async () => plan()),
  fetchPlan: vi.fn(async () => plan()),
  event: vi.fn(),
  rollback: vi.fn(),
  ...over,
});

describe('plans store', () => {
  it('load() sets the open plan; a terminal frame clears it', async () => {
    const ws = new FakeWsClient();
    const s = createPlansStore(ws, deps());
    await s.getState().load(PID);
    expect(s.getState().open?.id).toBe('p'.repeat(32));
    ws.emit({ type: 'plan-updated', projectId: PID, plan: plan({ status: 'rejected', updatedAt: '2026-10-18T10:01:00.000Z' }) });
    expect(s.getState().open).toBeNull();
    expect(s.getState().byId['p'.repeat(32)]?.status).toBe('rejected');
  });

  it('ignores frames of another project', async () => {
    const ws = new FakeWsClient();
    const s = createPlansStore(ws, deps({ fetchOpen: async () => null }));
    await s.getState().load(PID);
    ws.emit({ type: 'plan-updated', projectId: OTHER, plan: plan({ projectId: OTHER }) });
    expect(s.getState().byId).toEqual({});
  });

  it('refetches the open and unfinished plans on reconnect', async () => {
    const ws = new FakeWsClient();
    const d = deps();
    const s = createPlansStore(ws, d);
    await s.getState().load(PID);
    ws.emit({ type: 'plan-updated', projectId: PID, plan: plan({ id: 'q'.repeat(32), status: 'done' }) });
    // a card's plan that was open before the drop; the open plan is now another one
    ws.emit({ type: 'plan-updated', projectId: PID, plan: plan({ id: 'r'.repeat(32), status: 'paused' }) });
    ws.resume();
    await vi.waitFor(() => expect(d.fetchOpen).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(d.fetchPlan).toHaveBeenCalledTimes(1));
    expect(d.fetchPlan).toHaveBeenCalledWith(PID, 'r'.repeat(32)); // not the open one: loadOpen brought it
  });

  it('drops responses of the previous project after a switch', async () => {
    let finish: (p: PlanDTO | null) => void = () => {};
    const d = deps({ fetchOpen: vi.fn(() => new Promise<PlanDTO | null>((r) => (finish = r))) });
    const s = createPlansStore(new FakeWsClient(), d);
    const first = s.getState().load(PID);
    const late = finish;
    void s.getState().load(OTHER);
    late(plan());
    await first;
    expect(s.getState().projectId).toBe(OTHER);
    expect(s.getState().byId).toEqual({});
  });

  it('cards mounting together share one open-plan request', async () => {
    const d = deps();
    const s = createPlansStore(new FakeWsClient(), d);
    await s.getState().load(PID);
    await Promise.all([s.getState().load(PID), s.getState().ensure(PID, 'p'.repeat(32)), s.getState().ensure(PID, 'p'.repeat(32))]);
    expect(d.fetchOpen).toHaveBeenCalledTimes(2);
    expect(d.fetchPlan).not.toHaveBeenCalled();
  });
});
