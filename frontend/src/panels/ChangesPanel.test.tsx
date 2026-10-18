import { act, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChangeEntry, ChangeRun, ChangesDiff } from '@/api/changes';
import type { MessageDTO } from '@/protocol/frames';

const apiGet = vi.fn();
vi.mock('@/api/http', async (orig) => ({
  ...(await orig<typeof import('@/api/http')>()),
  apiGet: (...a: unknown[]) => apiGet(...a),
}));
const chat: { messages: MessageDTO[]; runActive: boolean } = { messages: [], runActive: false };
vi.mock('@/hooks/useChat', () => ({ useChat: () => chat }));

import { usePanels } from '@/layouts/panels';
import { ChangesStoreContext, createChangesStore } from '@/stores/changes';
import { createViewStore, ViewStoreContext } from '@/stores/view';
import { FakeWsClient } from '@/test/fakeWsClient';
import { ChangesPanel } from './ChangesPanel';

const PID = 'a1b2c3d4e5f6';
const ROOT = '/5b9623a5-6d01-41fc-9865-e1bc779418c8';
const SUB = `${ROOT}/00000000-0000-0000-0000-00004b3a1333`;
const SNAP1 = 'a'.repeat(32);
const SNAP2 = 'b'.repeat(32);

const run = (runId: string, over: Partial<ChangeRun> = {}): ChangeRun => ({
  runId, stepIds: [], count: 2, sideEffects: 0, erc: null, snapshotId: null, createdAt: '2026-10-18T10:00:00Z', ...over,
});
const entry = (id: number, over: Partial<ChangeEntry> = {}): ChangeEntry => ({
  id, runId: 'r2', planStepId: null, callSeq: id, tool: 'sch_place_symbol', op: 'add', kind: 'symbol',
  uuid: `uuid-${id}`, sheetPath: ROOT, ref: `R${id}`, net: null, before: null, after: null, sideEffect: false,
  createdAt: '2026-10-18T11:00:00Z', ...over,
});
const RUNS = [
  run('r2', { createdAt: '2026-10-18T11:00:00Z', count: 3, sideEffects: 1, snapshotId: SNAP2,
    erc: { errors: 2, warnings: 1, resultId: 'e'.repeat(32) }, stepIds: ['s1'] }),
  run('r1', { createdAt: '2026-10-18T10:00:00Z', snapshotId: SNAP1 }),
];
const ENTRIES = [
  entry(1, { planStepId: 's1' }),
  entry(2, { op: 'modify', ref: 'C105', uuid: 'uuid-c105', planStepId: 's1' }),
  entry(3, { op: 'delete', kind: 'wire', ref: null, uuid: 'uuid-w', sideEffect: true, sheetPath: SUB, tool: 'sch_delete' }),
];
const DIFF: ChangesDiff = {
  snapshotId: SNAP2,
  count: 2,
  sheets: {
    [ROOT]: [{ id: `${ROOT}/uuid-c105`, kind: 'modified', type: 'SCH_SYMBOL', uuid: 'uuid-c105', ref: 'C105',
      boxMm: { x: 166.5, y: 65.66, w: 16.85, h: 8.38 }, properties: [{ name: 'Value', before: '10uF', after: '22uF' }], sideEffect: false }],
    [SUB]: [{ id: `${SUB}/uuid-w`, kind: 'removed', type: 'SCH_LINE', uuid: 'uuid-w', ref: null,
      boxMm: { x: 99.92, y: 99.92, w: 10.15, h: 0.15 }, properties: [], sideEffect: true }],
  },
};
const ERC = {
  id: 'e'.repeat(32), runId: 'r2', planStepId: null, baselineSnapshotId: SNAP2, createdAt: '2026-10-18T11:01:00Z',
  newErrors: [
    { id: 'v1', type: 'pin_not_connected', severity: 'error', description: 'Pin not connected' },
    { id: 'v2', type: 'power_pin_not_driven', severity: 'error', description: 'Input Power pin not driven' },
  ],
  newWarnings: [{ id: 'v3', type: 'lib_symbol_mismatch', severity: 'warning', description: 'Symbol differs' }],
  resolved: [],
  justifications: { v2: 'driven by the connector on the next sheet' },
};

function routes(path: string): unknown {
  if (path.startsWith(`/projects/${PID}/changes/runs`)) return RUNS;
  if (path.startsWith(`/projects/${PID}/changes/diff`)) return DIFF;
  if (path.startsWith(`/projects/${PID}/changes?runId=r2`)) return ENTRIES;
  if (path.startsWith(`/projects/${PID}/changes?runId=r1`)) return [entry(9, { runId: 'r1' })];
  if (path.startsWith(`/projects/${PID}/erc-results/`)) return ERC;
  throw new Error(`unexpected ${path}`);
}

function setup() {
  const ws = new FakeWsClient();
  const changes = createChangesStore(ws);
  const view = createViewStore(ws);
  view.getState().setProject(PID);
  render(
    <ViewStoreContext.Provider value={view}>
      <ChangesStoreContext.Provider value={changes}>
        <ChangesPanel projectId={PID} />
      </ChangesStoreContext.Provider>
    </ViewStoreContext.Provider>,
  );
  return { ws, changes, view };
}

const notice = (id: number, createdAt: string, snapshotId: string): MessageDTO => ({
  id, projectId: PID, role: 'system', kind: 'notice', content: 'Restored', status: 'complete',
  metadata: { action: 'restored', snapshotId }, createdAt,
} as unknown as MessageDTO);

const runItem = (runId: string) => screen.getByRole('listitem', { name: new RegExp(`Turn ${runId}`) });

beforeEach(() => {
  apiGet.mockReset().mockImplementation(async (p: string) => routes(p));
  chat.messages = [];
});

describe('ChangesPanel', () => {
  it('is registered as "changes" (order 15, after history)', async () => {
    await import('./index');
    const { result } = renderHook(() => usePanels());
    const def = result.current.find((p) => p.id === 'changes');
    expect(def).toMatchObject({ title: 'Changes', order: 15 });
  });

  it('lists runs newest first with count, side effects and the ERC badge', async () => {
    setup();
    const items = await screen.findAllByRole('listitem', { name: /^Turn/ });
    expect(items[0]).toHaveTextContent('3 changes');
    expect(items[0]).toHaveTextContent('1 side effect');
    expect(items[1]).toHaveTextContent('2 changes');
    const errors = within(items[0]!).getByRole('button', { name: /2 new ERC errors/ });
    expect(errors.className).toMatch(/danger/);
    expect(within(items[0]!).getByText(/1 warning/).className).toMatch(/warning/);
  });

  it('ERC badge details show justified errors with their reason', async () => {
    setup();
    await screen.findAllByRole('listitem', { name: /^Turn/ });
    fireEvent.click(within(runItem('r2')).getByRole('button', { name: /2 new ERC errors/ }));
    expect(await screen.findByText('Pin not connected')).toBeInTheDocument();
    expect(screen.getByText(/Justified: driven by the connector on the next sheet/)).toBeInTheDocument();
    expect(apiGet).toHaveBeenCalledWith(`/projects/${PID}/erc-results/${'e'.repeat(32)}`);
  });

  it('expanding a run groups entries by step and marks side effects', async () => {
    setup();
    await screen.findAllByRole('listitem', { name: /^Turn/ });
    fireEvent.click(within(runItem('r2')).getByRole('button', { name: /Show changes/ }));
    const step = await screen.findByRole('group', { name: /Plan step 1/ });
    expect(within(step).getByRole('button', { name: /Added symbol R1/ })).toBeInTheDocument();
    expect(within(step).getByRole('button', { name: /Modified symbol C105/ })).toBeInTheDocument();
    const turn = screen.getByRole('group', { name: /Outside a plan step/ });
    const wire = within(turn).getByRole('button', { name: /Deleted wire/ });
    expect(wire).toHaveTextContent('side effect');
  });

  it('tapping an entry highlights it on its sheet', async () => {
    const { view } = setup();
    await screen.findAllByRole('listitem', { name: /^Turn/ });
    fireEvent.click(within(runItem('r2')).getByRole('button', { name: /Show changes/ }));
    fireEvent.click(await screen.findByRole('button', { name: /Modified symbol C105/ }));
    expect(view.getState().highlight).toMatchObject({
      refs: [{ ref: 'C105', kind: 'modified', sheetPath: ROOT }], boxes: [], sheetPath: ROOT,
    });
    expect(view.getState().mode).toBe('kicad');
  });

  it('"Show all changes" highlights every entry of the run, each on its sheet', async () => {
    const { view } = setup();
    await screen.findAllByRole('listitem', { name: /^Turn/ });
    fireEvent.click(within(runItem('r2')).getByRole('button', { name: /Show changes/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Show all changes' }));
    const h = view.getState().highlight!;
    expect(h.sheetPath).toBe(ROOT);
    expect(h.refs).toEqual([
      { ref: 'R1', kind: 'added', sheetPath: ROOT },
      { ref: 'C105', kind: 'modified', sheetPath: ROOT },
      { ref: 'uuid-w', kind: 'removed', sheetPath: SUB },
    ]);
  });

  it('Diff fetches the diff of the run snapshot and highlights boxes by kind; off clears', async () => {
    const { view } = setup();
    await screen.findAllByRole('listitem', { name: /^Turn/ });
    fireEvent.click(within(runItem('r2')).getByRole('button', { name: /Show changes/ }));
    const diffBtn = await screen.findByRole('button', { name: 'Diff' });
    fireEvent.click(diffBtn);
    await waitFor(() => expect(view.getState().highlight?.boxes).toHaveLength(2));
    expect(apiGet).toHaveBeenCalledWith(`/projects/${PID}/changes/diff?snapshotId=${SNAP2}`);
    expect(view.getState().highlight!.boxes).toEqual([
      { x: 166.5, y: 65.66, w: 16.85, h: 8.38, kind: 'modified', sheetPath: ROOT },
      { x: 99.92, y: 99.92, w: 10.15, h: 0.15, kind: 'removed', sheetPath: SUB },
    ]);
    expect(diffBtn).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText(/Value: 10uF → 22uF/)).toBeInTheDocument();
    // with the diff loaded, a deleted wire is highlighted by its box
    fireEvent.click(screen.getByRole('button', { name: /Deleted wire/ }));
    expect(view.getState().highlight).toMatchObject({ refs: [], boxes: [{ kind: 'removed', sheetPath: SUB }] });
    fireEvent.click(diffBtn);
    expect(view.getState().highlight).toBeNull();
  });

  it('Before / after shows the snapshot preview against the current agent view', async () => {
    setup();
    await screen.findAllByRole('listitem', { name: /^Turn/ });
    fireEvent.click(within(runItem('r2')).getByRole('button', { name: /Show changes/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Before / after' }));
    const before = screen.getByRole('img', { name: 'Schematic before this turn' });
    expect(before.getAttribute('src')).toBe(`/api/projects/${PID}/history/${SNAP2}/preview`);
    fireEvent.click(screen.getByRole('button', { name: 'After' }));
    const after = screen.getByRole('img', { name: 'Schematic now' });
    expect(after.getAttribute('src')).toMatch(new RegExp(`^/api/projects/${PID}/agent-view\\?doc=sch`));
  });

  it('a later restore is a boundary: runs it undid are marked, diff side effects relabelled', async () => {
    chat.messages = [notice(50, '2026-10-18T12:00:00Z', SNAP2)];
    setup();
    await screen.findAllByRole('listitem', { name: /^Turn/ });
    expect(screen.getByRole('listitem', { name: /Restored/ })).toBeInTheDocument();
    expect(runItem('r2')).toHaveTextContent('Undone by a restore');
    expect(runItem('r1')).not.toHaveTextContent('Undone by a restore');
    fireEvent.click(within(runItem('r2')).getByRole('button', { name: /Show changes/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Diff' }));
    expect(await screen.findByText(/restored after this turn/i)).toBeInTheDocument();
    const wire = screen.getByRole('button', { name: /Removed SCH_LINE/ });
    expect(wire).toHaveTextContent('restore or side effect');
  });

  it('refetches the runs after a changes-updated frame', async () => {
    const { ws } = setup();
    await screen.findAllByRole('listitem', { name: /^Turn/ });
    apiGet.mockClear();
    await act(async () => {
      ws.emit({ type: 'changes-updated', projectId: PID, runId: 'r2', stepId: null, count: 4 });
      await new Promise((r) => setTimeout(r, 350));
    });
    expect(apiGet).toHaveBeenCalledWith(`/projects/${PID}/changes/runs?limit=20`);
  });

  it('explains an empty change log', async () => {
    apiGet.mockImplementation(async () => []);
    setup();
    expect(await screen.findByText(/No changes yet/)).toBeInTheDocument();
  });
});
