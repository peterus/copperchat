import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

const apiPost = vi.fn();
vi.mock('@/api/http', async (orig) => ({
  ...(await orig<typeof import('@/api/http')>()),
  apiPost: (...a: unknown[]) => apiPost(...a),
}));

import { ApiError } from '@/api/http';
import type { PlanDTO } from '@/protocol/frames';
import { createPlansStore, PlansStoreContext } from '@/stores/plans';
import { FakeWsClient } from '@/test/fakeWsClient';
import { PID, row } from '@/test/fakeMessageServer';
import { MessageItem } from './MessageItem';

const PLAN = 'p'.repeat(32);
const plan: PlanDTO = {
  id: PLAN, projectId: PID, title: 'Add a status LED', summary: '', status: 'proposed', revision: 1, pauseReason: null,
  steps: [], createdAt: '2026-10-18T10:00:00.000Z', updatedAt: '2026-10-18T10:00:00.000Z',
};
const notice = (metadata: Record<string, unknown>, content = 'notice text') =>
  row(7, { role: 'system', kind: 'notice', content, metadata });

describe('MessageItem editing notices', () => {
  it('a plan notice renders the plan card', async () => {
    const fetchPlan = vi.fn(async () => plan);
    const store = createPlansStore(new FakeWsClient(), { fetchOpen: async () => plan, fetchPlan, event: vi.fn(), rollback: vi.fn() });
    render(
      <PlansStoreContext.Provider value={store}>
        <MessageItem message={notice({ action: 'plan', planId: PLAN }, 'The agent proposed a plan.')} />
      </PlansStoreContext.Provider>,
    );
    expect(await screen.findByRole('heading', { name: 'Add a status LED' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument();
  });

  it('a plan notice without a plan id stays a plain notice', () => {
    render(<MessageItem message={notice({ action: 'plan' }, 'The agent proposed a plan.')} />);
    expect(screen.getByText('The agent proposed a plan.')).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('an erc-delta notice shows the ERC badge', () => {
    render(<MessageItem message={notice({ action: 'erc-delta', ercResultId: 'e'.repeat(32), errors: 1, warnings: 2 }, 'ERC after this turn')} />);
    expect(screen.getByText('ERC after this turn')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '1 new ERC error, 2 warnings' })).toBeInTheDocument();
  });

  it('an erc-delta notice with bad counts has no badge', () => {
    render(<MessageItem message={notice({ action: 'erc-delta', ercResultId: 'e'.repeat(32), errors: 'x', warnings: 2 })} />);
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('a restore notice for a step checkpoint is labelled so', () => {
    render(<MessageItem message={notice({ action: 'restore', snapshotId: 'c'.repeat(32), checkpoint: true })} />);
    expect(screen.getByRole('button', { name: 'Restore step checkpoint' })).toBeInTheDocument();
  });

  it('a restore notice for a turn snapshot keeps its label', () => {
    render(<MessageItem message={notice({ action: 'restore', snapshotId: 'c'.repeat(32) })} />);
    expect(screen.getByRole('button', { name: 'Restore snapshot' })).toBeInTheDocument();
  });

  it('a checkpoint restore refused because a plan is open points to the plan card', async () => {
    apiPost.mockRejectedValue(new ApiError(409, { error: { code: 'plan_open', message: 'The design cannot be restored while a plan is open' } }, 'The design cannot be restored while a plan is open'));
    render(<MessageItem message={notice({ action: 'restore', snapshotId: 'c'.repeat(32), checkpoint: true })} />);
    fireEvent.click(screen.getByRole('button', { name: 'Restore step checkpoint' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('at the end of “the last finished plan step”');
    fireEvent.click(screen.getByRole('button', { name: /^restore$/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Roll back to…');
  });
});
