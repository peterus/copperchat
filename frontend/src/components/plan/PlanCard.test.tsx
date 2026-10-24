import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/api/http';
import { ChatStoreContext } from '@/hooks/useChat';
import type { PlanDTO, PlanStepDTO } from '@/protocol/frames';
import { createChatStore } from '@/stores/chat';
import { createPlansStore, type PlansDeps, PlansStoreContext } from '@/stores/plans';
import { FakeWsClient } from '@/test/fakeWsClient';
import { ARM_MS, PlanCard } from './PlanCard';

const PID = 'a1b2c3d4e5f6';
const CID = '0123456789abcdef0123456789abcdef';
const PLAN = 'p'.repeat(32);
const CP1 = 'c'.repeat(32);

const step = (idx: number, over: Partial<PlanStepDTO> = {}): PlanStepDTO => ({
  idx,
  title: `Step ${idx} title`,
  intent: `Intent ${idx}`,
  expectedChanges: 3,
  touches: { refs: [], nets: [] },
  verification: 'ERC clean',
  status: 'pending',
  changeCount: 0,
  used: 0,
  checkpointId: null,
  summary: null,
  failure: null,
  erc: null,
  ...over,
});
const plan = (over: Partial<PlanDTO> = {}): PlanDTO => ({
  id: PLAN,
  projectId: PID,
  conversationId: CID,
  title: 'Add a status LED',
  summary: 'Place R and LED, connect to GPIO2.',
  status: 'proposed',
  revision: 1,
  pauseReason: null,
  reviseReason: null,
  previousRevision: null,
  steps: [step(1), step(2)],
  createdAt: '2026-10-18T10:00:00.000Z',
  updatedAt: '2026-10-18T10:00:00.000Z',
  ...over,
});

function setup(initial: PlanDTO, deps: Partial<PlansDeps> = {}) {
  const ws = new FakeWsClient();
  const fullDeps: PlansDeps = {
    fetchOpen: vi.fn(async () => (['done', 'rejected', 'aborted'].includes(initial.status) ? null : initial)),
    fetchPlan: vi.fn(async () => initial),
    event: vi.fn(async (_p, _id, e) => plan({ status: e === 'approve' ? 'approved' : e === 'reject' ? 'rejected' : 'aborted', updatedAt: '2026-10-18T10:01:00.000Z' })),
    rollback: vi.fn(async () => plan({ status: 'paused', updatedAt: '2026-10-18T10:02:00.000Z' })),
    ...deps,
  };
  const plans = createPlansStore(ws, fullDeps);
  const chat = createChatStore({ ws, fetchPage: async () => ({ messages: [], hasMore: false }) });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <ChatStoreContext.Provider value={chat}>
      <PlansStoreContext.Provider value={plans}>{children}</PlansStoreContext.Provider>
    </ChatStoreContext.Provider>
  );
  const view = render(<PlanCard projectId={PID} planId={PLAN} />, { wrapper });
  return { ws, plans, chat, deps: fullDeps, view };
}

const busy = () => new ApiError(409, { error: { code: 'agent_busy', message: 'the agent is working' } }, 'the agent is working');

describe('PlanCard', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('proposed shows Approve and Reject with title, summary and numbered steps', async () => {
    setup(plan());
    expect(await screen.findByRole('heading', { name: 'Add a status LED' })).toBeInTheDocument();
    expect(screen.getByText('Place R and LED, connect to GPIO2.')).toBeInTheDocument();
    const steps = screen.getAllByRole('listitem');
    expect(steps).toHaveLength(2);
    expect(within(steps[0]!).getByText('Step 1 title')).toBeInTheDocument();
    expect(within(steps[0]!).getByText(/3 changes expected/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Reject' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull();
  });

  it('approve posts once and disables buttons while pending', async () => {
    let finish: (p: PlanDTO) => void = () => {};
    const event = vi.fn(() => new Promise<PlanDTO>((r) => (finish = r)));
    const { deps } = setup(plan(), { event });
    const approve = await screen.findByRole('button', { name: 'Approve' });
    fireEvent.click(approve);
    fireEvent.click(approve);
    expect(deps.event).toHaveBeenCalledTimes(1);
    expect(deps.event).toHaveBeenCalledWith(PID, PLAN, 'approve');
    expect(screen.getByRole('button', { name: /Approv/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Reject' })).toBeDisabled();
    await act(async () => finish(plan({ status: 'approved', updatedAt: '2026-10-18T10:01:00.000Z' })));
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
    expect(screen.getByText(/Approved/)).toBeInTheDocument();
  });

  it('running shows Stop (interrupt frame) and per-step status', async () => {
    const { ws, chat } = setup(
      plan({ status: 'running', steps: [step(1, { status: 'done', changeCount: 2, checkpointId: CP1 }), step(2, { status: 'running' })] }),
    );
    await act(async () => {
      await chat.getState().open(PID, CID);
      ws.emit({ type: 'run-started', projectId: PID, conversationId: CID, runId: 'r1' });
    });
    const items = await screen.findAllByRole('listitem');
    expect(within(items[0]!).getByText('Done')).toBeInTheDocument();
    expect(within(items[1]!).getByText('Running')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(ws.framesOfType('interrupt')).toEqual([{ type: 'interrupt', projectId: PID, conversationId: CID, runId: 'r1' }]);
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
  });

  it('paused shows Resume, Roll back to…, Abort; rollback confirms with the checkpoints', async () => {
    const rollback = vi.fn().mockRejectedValueOnce(busy()).mockResolvedValue(plan({ status: 'paused', updatedAt: '2026-10-18T10:05:00.000Z' }));
    const { deps } = setup(
      plan({
        status: 'paused',
        pauseReason: 'turn_ended',
        steps: [step(1, { status: 'done', checkpointId: CP1, changeCount: 4 }), step(2, { status: 'interrupted' })],
      }),
      { rollback },
    );
    expect(await screen.findByRole('button', { name: 'Resume' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Abort' })).toBeEnabled();
    expect(screen.getByText(/the turn ended/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Roll back to…' }));
    const dialog = screen.getByRole('dialog', { name: /Roll back/ });
    const options = within(dialog).getAllByRole('radio');
    expect(options.map((o) => (o as HTMLInputElement).value)).toEqual(['0', '1']);
    expect(within(dialog).getByLabelText(/After step 1: Step 1 title/)).toBeChecked();
    expect(within(dialog).getByLabelText(/Start/)).not.toBeChecked();
    expect(deps.rollback).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Roll back' }));
    await waitFor(() => expect(deps.rollback).toHaveBeenCalledWith(PID, PLAN, 1));
    expect(await screen.findByRole('alert')).toHaveTextContent(/agent is working/i);
    expect(screen.queryByRole('dialog')).toBeNull();
    // retry to the start
    fireEvent.click(screen.getByRole('button', { name: 'Roll back to…' }));
    fireEvent.click(screen.getByLabelText(/Start/));
    fireEvent.click(screen.getByRole('button', { name: 'Roll back' }));
    await waitFor(() => expect(deps.rollback).toHaveBeenLastCalledWith(PID, PLAN, 0));
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  });

  it('resume refused with 409 plan_state shows the reason and refetches the plan', async () => {
    const event = vi.fn().mockRejectedValue(new ApiError(409, { error: { code: 'plan_state', message: 'Only a paused plan can be resumed.' } }, 'Only a paused plan can be resumed.'));
    const fetchPlan = vi.fn().mockResolvedValue(plan({ status: 'aborted', updatedAt: '2026-10-18T11:00:00.000Z' }));
    setup(plan({ status: 'paused', pauseReason: 'interrupted' }), { event, fetchPlan });
    fireEvent.click(await screen.findByRole('button', { name: 'Resume' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Only a paused plan can be resumed.');
    await waitFor(() => expect(fetchPlan).toHaveBeenCalledWith(PID, PLAN));
    expect(await screen.findByText('Aborted')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull();
  });

  it('reject needs a second tap', async () => {
    const { deps } = setup(plan());
    fireEvent.click(await screen.findByRole('button', { name: 'Reject' }));
    expect(deps.event).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Really reject?' }));
    await waitFor(() => expect(deps.event).toHaveBeenCalledWith(PID, PLAN, 'reject'));
  });

  it('abort needs a second tap', async () => {
    const { deps } = setup(plan({ status: 'paused', pauseReason: 'interrupted' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Abort' }));
    expect(deps.event).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Really abort?' }));
    await waitFor(() => expect(deps.event).toHaveBeenCalledWith(PID, PLAN, 'abort'));
  });

  it('an armed abort disarms after a while', async () => {
    vi.useFakeTimers();
    try {
      const { deps } = setup(plan({ status: 'approved' }));
      await act(async () => {
        await vi.runOnlyPendingTimersAsync();
      });
      fireEvent.click(screen.getByRole('button', { name: 'Abort' }));
      act(() => vi.advanceTimersByTime(ARM_MS + 1));
      fireEvent.click(screen.getByRole('button', { name: 'Abort' }));
      expect(deps.event).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('a plan_state refusal refetches the plan', async () => {
    const event = vi.fn().mockRejectedValue(new ApiError(409, { error: { code: 'plan_state', message: 'Only a proposed plan can be approved.' } }, 'Only a proposed plan can be approved.'));
    const fetchPlan = vi.fn().mockResolvedValueOnce(plan()).mockResolvedValue(plan({ status: 'rejected', updatedAt: '2026-10-18T11:00:00.000Z' }));
    setup(plan(), { event, fetchPlan, fetchOpen: async () => null });
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Only a proposed plan can be approved.');
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull());
    expect(screen.getByText('Rejected')).toBeInTheDocument();
  });

  it('done step shows checkpoint link, change count, ERC justifications (truncated)', async () => {
    const long = 'x'.repeat(400);
    const justifications: Record<string, string> = { v1: 'Unused input on purpose', v2: long };
    for (let i = 3; i <= 9; i++) justifications[`v${i}`] = `reason ${i}`;
    setup(
      plan({
        status: 'running',
        steps: [
          step(1, { status: 'done', changeCount: 4, checkpointId: CP1, summary: 'Placed R5 and D2', erc: { errors: 1, warnings: 2, justifications, rules: [], resultId: 'e'.repeat(32) } }),
          step(2, { status: 'running' }),
        ],
      }),
    );
    const first = (await screen.findAllByRole('listitem'))[0]!;
    expect(within(first).getByText('4 changes')).toBeInTheDocument();
    expect(within(first).getByText('Placed R5 and D2')).toBeInTheDocument();
    expect(within(first).getByText(/1 ERC error/)).toBeInTheDocument();
    fireEvent.click(within(first).getByText(/9 justified/));
    expect(within(first).getByText('Unused input on purpose')).toBeInTheDocument();
    expect(within(first).getByText(`${'x'.repeat(199)}…`)).toBeInTheDocument();
    expect(within(first).getByText('+4 more')).toBeInTheDocument();
    fireEvent.click(within(first).getByRole('button', { name: 'Checkpoint' }));
    const dialog = screen.getByRole('dialog', { name: /Checkpoint after step 1/ });
    const img = within(dialog).getByRole('img');
    expect(img.getAttribute('src')).toBe(`/api/projects/${PID}/history/${CP1}/preview`);
    fireEvent.error(img); // no preview rendered, or pruned
    expect(within(dialog).getByText('The image is not available.')).toBeInTheDocument();
  });

  it('done step lists pattern rules with the number of errors they covered', async () => {
    const rules = [{ match: { type: 'pin_not_connected', sheet: 'Power' }, reason: 'wired in step 3', covered: 12 }];
    setup(
      plan({
        status: 'running',
        steps: [
          step(1, { status: 'done', checkpointId: CP1, erc: { errors: 12, warnings: 0, justifications: { v1: 'one id' }, rules, resultId: 'e'.repeat(32) } }),
          step(2, { status: 'running' }),
        ],
      }),
    );
    const first = (await screen.findAllByRole('listitem'))[0]!;
    fireEvent.click(within(first).getByText(/13 justified/));
    expect(within(first).getByText(/pin_not_connected · Power: wired in step 3 \(12 errors\)/)).toBeInTheDocument();
    expect(within(first).getByText('one id')).toBeInTheDocument();
  });

  it('failed step shows its failure', async () => {
    setup(plan({ status: 'paused', pauseReason: 'step_exceeded', steps: [step(1, { status: 'failed', failure: 'step exceeded 15 changes' })] }));
    expect(await screen.findByText('step exceeded 15 changes')).toBeInTheDocument();
    expect(screen.getByText('Failed')).toBeInTheDocument();
  });

  it('card updates from plan-updated without reload', async () => {
    const { ws, deps } = setup(plan());
    await screen.findByRole('button', { name: 'Approve' });
    act(() => ws.emit({ type: 'plan-updated', projectId: PID, plan: plan({ status: 'running', updatedAt: '2026-10-18T10:03:00.000Z', steps: [step(1, { status: 'running' }), step(2)] }) }));
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
    expect(screen.getByText('Running', { selector: '[data-plan-status]' })).toBeInTheDocument();
    // an older copy never wins
    act(() => ws.emit({ type: 'plan-updated', projectId: PID, plan: plan({ updatedAt: '2026-10-18T10:02:00.000Z' }) }));
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
    expect(deps.fetchPlan).not.toHaveBeenCalled();
  });

  it('revision badge after plan_revise', async () => {
    const { ws } = setup(plan());
    await screen.findByRole('button', { name: 'Approve' });
    expect(screen.queryByText(/Revision/)).toBeNull();
    act(() => ws.emit({ type: 'plan-updated', projectId: PID, plan: plan({ revision: 2, updatedAt: '2026-10-18T10:04:00.000Z', steps: [step(1, { title: 'Revised step' })] }) }));
    expect(screen.getByText('Revision 2')).toBeInTheDocument();
    expect(screen.getByText('Revised step')).toBeInTheDocument();
  });

  it('an armed reject disarms when the plan gets a new revision with the same status', async () => {
    const { ws, deps } = setup(plan());
    fireEvent.click(await screen.findByRole('button', { name: 'Reject' }));
    expect(screen.getByRole('button', { name: 'Really reject?' })).toBeInTheDocument();
    act(() => ws.emit({ type: 'plan-updated', projectId: PID, plan: plan({ revision: 2, updatedAt: '2026-10-18T10:04:00.000Z' }) }));
    expect(screen.queryByRole('button', { name: 'Really reject?' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }));
    expect(deps.event).not.toHaveBeenCalled();
  });

  it('an armed abort disarms when the plan gets a new revision with the same status', async () => {
    const { ws } = setup(plan({ status: 'approved' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Abort' }));
    expect(screen.getByRole('button', { name: 'Really abort?' })).toBeInTheDocument();
    act(() => ws.emit({ type: 'plan-updated', projectId: PID, plan: plan({ status: 'approved', revision: 2, updatedAt: '2026-10-18T10:04:00.000Z' }) }));
    expect(screen.queryByRole('button', { name: 'Really abort?' })).toBeNull();
  });

  it.each(['done', 'rejected', 'aborted'] as const)('terminal state %s shows no buttons', async (status) => {
    setup(plan({ status }));
    await screen.findByRole('heading', { name: 'Add a status LED' });
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });

  it('fetches a plan that is not the open one', async () => {
    const { deps } = setup(plan({ status: 'done' }));
    await screen.findByRole('heading', { name: 'Add a status LED' });
    expect(deps.fetchPlan).toHaveBeenCalledWith(PID, PLAN);
  });

  it('a load failure is shown', async () => {
    setup(plan(), { fetchOpen: async () => null, fetchPlan: async () => Promise.reject(new Error('HTTP 500')) });
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load the plan: HTTP 500');
  });

  it('paused step shows "Paused – revision awaiting approval" and the reason', async () => {
    setup(
      plan({
        revision: 2,
        previousRevision: 1,
        reviseReason: 'The LED needs <b>a second</b> resistor',
        steps: [step(1, { status: 'done', changeCount: 2 }), step(2, { status: 'paused', used: 2, expectedChanges: 5 })],
      }),
    );
    const items = await screen.findAllByRole('listitem');
    expect(within(items[1]!).getByText('Paused – revision awaiting approval')).toBeInTheDocument();
    // Agent-authored: plain text, never markup.
    expect(within(items[1]!).getByText("Agent's reason: The LED needs <b>a second</b> resistor")).toBeInTheDocument();
    expect(within(items[1]!).getByText('2 of 5 changes used')).toBeInTheDocument();
    expect(items[1]!.querySelector('b')).toBeNull();
  });

  it('a paused step of a paused plan just shows "Paused"', async () => {
    setup(
      plan({
        status: 'paused',
        pauseReason: 'interrupted',
        reviseReason: 'old reason',
        steps: [step(1, { status: 'paused', used: 1 })],
      }),
    );
    const item = (await screen.findAllByRole('listitem'))[0]!;
    expect(within(item).getByText('Paused')).toBeInTheDocument();
    expect(within(item).queryByText(/awaiting approval/)).toBeNull();
    expect(within(item).queryByText('old reason')).toBeNull();
  });

  it('an interrupted step shows the changes it already used', async () => {
    setup(plan({ status: 'paused', pauseReason: 'turn_ended', steps: [step(1, { status: 'interrupted', used: 2, expectedChanges: 5 })] }));
    const item = (await screen.findAllByRole('listitem'))[0]!;
    expect(within(item).getByText('2 of 5 changes used')).toBeInTheDocument();
  });

  it('rejecting a mid-step revision says it returns to the previous revision', async () => {
    const { deps } = setup(plan({ revision: 3, previousRevision: 2, steps: [step(1, { status: 'paused' })] }));
    const reject = await screen.findByRole('button', { name: 'Reject revision' });
    expect(screen.getByText(/Rejecting returns the plan to revision 2, paused/)).toBeInTheDocument();
    fireEvent.click(reject);
    expect(deps.event).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Really reject revision?' }));
    await waitFor(() => expect(deps.event).toHaveBeenCalledWith(PID, PLAN, 'reject'));
  });

  it('a revision without a previous revision keeps the plain Reject', async () => {
    setup(plan({ revision: 2 }));
    expect(await screen.findByRole('button', { name: 'Reject' })).toBeInTheDocument();
    expect(screen.queryByText(/Rejecting returns/)).toBeNull();
  });

  it('read-only shows the plan without action buttons', async () => {
    const ws = new FakeWsClient();
    const plans = createPlansStore(ws, { fetchOpen: async () => plan(), fetchPlan: vi.fn(), event: vi.fn(), rollback: vi.fn() });
    render(
      <PlansStoreContext.Provider value={plans}>
        <PlanCard projectId={PID} planId={PLAN} readOnly />
      </PlansStoreContext.Provider>,
    );
    expect(await screen.findByRole('heading', { name: 'Add a status LED' })).toBeInTheDocument();
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });

  it('phone layout: buttons full width', async () => {
    setup(plan());
    const approve = await screen.findByRole('button', { name: 'Approve' });
    expect(approve.className).toMatch(/(^| )w-full( |$)/);
    expect(approve.className).toMatch(/sm:w-auto/);
  });
});
