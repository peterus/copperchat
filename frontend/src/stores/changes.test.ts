import { afterEach, beforeEach, describe, expect, it, type Mock, vi } from 'vitest';
import type { ChangeEntry, ChangeRun } from '@/api/changes';
import { FakeWsClient } from '@/test/fakeWsClient';
import { CHANGES_DEBOUNCE_MS, type ChangesDeps, createChangesStore, entriesKey } from './changes';

const P1 = 'a1b2c3d4e5f6';
const P2 = 'b1b2c3d4e5f6';

const run = (runId: string, over: Partial<ChangeRun> = {}): ChangeRun => ({
  runId, stepIds: [], count: 1, sideEffects: 0, erc: null, snapshotId: null, createdAt: '2026-10-18T10:00:00Z', ...over,
});
const entry = (id: number, runId: string): ChangeEntry => ({
  id, runId, planStepId: null, callSeq: 1, tool: 'sch_place_symbol', op: 'add', kind: 'symbol', uuid: `u${id}`,
  sheetPath: '/root', ref: `R${id}`, net: null, before: null, after: null, sideEffect: false, createdAt: '2026-10-18T10:00:00Z',
});

let ws: FakeWsClient;
let fetchRuns: Mock<ChangesDeps['fetchRuns']>;
let fetchEntries: Mock<ChangesDeps['fetchEntries']>;
let store: ReturnType<typeof createChangesStore>;

const flush = async () => {
  await vi.advanceTimersByTimeAsync(CHANGES_DEBOUNCE_MS + 1);
};

beforeEach(() => {
  vi.useFakeTimers();
  ws = new FakeWsClient();
  fetchRuns = vi.fn<ChangesDeps['fetchRuns']>().mockResolvedValue([run('r1')]);
  fetchEntries = vi.fn<ChangesDeps['fetchEntries']>().mockResolvedValue([entry(1, 'r1')]);
  store = createChangesStore(ws, { fetchRuns, fetchEntries });
});
afterEach(() => {
  store.getState().dispose();
  vi.useRealTimers();
});

describe('changes store', () => {
  it('load fetches the runs of the project', async () => {
    await store.getState().load(P1);
    expect(fetchRuns).toHaveBeenCalledWith(P1);
    expect(store.getState().runs).toEqual([run('r1')]);
    expect(store.getState().error).toBeNull();
  });

  it('a burst of changes-updated frames refetches the runs once (debounced)', async () => {
    await store.getState().load(P1);
    fetchRuns.mockClear();
    for (let i = 0; i < 5; i++) ws.emit({ type: 'changes-updated', projectId: P1, runId: 'r1', stepId: null, count: i + 1 });
    expect(fetchRuns).not.toHaveBeenCalled();
    await flush();
    expect(fetchRuns).toHaveBeenCalledTimes(1);
  });

  it('refreshes after run-finished and ignores other projects', async () => {
    await store.getState().load(P1);
    fetchRuns.mockClear();
    ws.emit({ type: 'changes-updated', projectId: P2, runId: 'r1', stepId: null, count: 1 });
    await flush();
    expect(fetchRuns).not.toHaveBeenCalled();
    ws.emit({ type: 'run-finished', projectId: P1, runId: 'r1', reason: 'completed' });
    await flush();
    expect(fetchRuns).toHaveBeenCalledTimes(1);
  });

  it('loadEntries caches per run/step; a frame for the run refetches loaded entries', async () => {
    await store.getState().load(P1);
    await store.getState().loadEntries('r1');
    await store.getState().loadEntries('r1', 's1');
    expect(fetchEntries).toHaveBeenCalledWith(P1, 'r1', null);
    expect(fetchEntries).toHaveBeenCalledWith(P1, 'r1', 's1');
    expect(store.getState().entries[entriesKey('r1')]).toEqual([entry(1, 'r1')]);
    fetchEntries.mockClear().mockResolvedValue([entry(1, 'r1'), entry(2, 'r1')]);
    ws.emit({ type: 'changes-updated', projectId: P1, runId: 'r1', stepId: 's1', count: 2 });
    await flush();
    expect(fetchEntries).toHaveBeenCalledTimes(2);
    expect(store.getState().entries[entriesKey('r1')]).toHaveLength(2);
  });

  it('keeps an error message and the previous runs when a refetch fails', async () => {
    await store.getState().load(P1);
    fetchRuns.mockRejectedValueOnce(new Error('boom'));
    ws.emit({ type: 'changes-updated', projectId: P1, runId: 'r1', stepId: null, count: 1 });
    await flush();
    expect(store.getState().runs).toEqual([run('r1')]);
    expect(store.getState().error).toMatch(/boom/);
  });

  it('a project switch drops the old data and ignores late responses of the old project', async () => {
    let resolveOld: (v: ChangeRun[]) => void = () => {};
    fetchRuns.mockImplementationOnce(() => new Promise((r) => (resolveOld = r)));
    const old = store.getState().load(P1);
    fetchRuns.mockResolvedValueOnce([run('r9')]);
    await store.getState().load(P2);
    resolveOld([run('r1')]);
    await old;
    expect(store.getState().projectId).toBe(P2);
    expect(store.getState().runs).toEqual([run('r9')]);
    expect(store.getState().entries).toEqual({});
  });

  it('the latest request wins when two fetches of the runs resolve in reverse order', async () => {
    await store.getState().load(P1);
    const resolvers: Array<(v: ChangeRun[]) => void> = [];
    fetchRuns.mockImplementation(() => new Promise((r) => resolvers.push(r)));
    const first = store.getState().load(P1);
    ws.emit({ type: 'run-finished', projectId: P1, runId: 'r1', reason: 'completed' });
    await flush();
    expect(resolvers).toHaveLength(2);
    resolvers[1]!([run('r1'), run('r2')]);
    await vi.advanceTimersByTimeAsync(0);
    resolvers[0]!([run('r1')]); // older response, late
    await first;
    expect(store.getState().runs).toEqual([run('r1'), run('r2')]);
  });
});
