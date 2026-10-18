import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiPost = vi.fn();
vi.mock('@/api/http', () => ({ apiPost: (...a: unknown[]) => apiPost(...a) }));

import { planOpenRestoreHint, restoreSnapshot } from './restore';

/** The 409 body `POST /history/{id}/restore` sends while a plan is open (routes_history.py). */
const planOpen = (status: string, why = `Plan ${'p'.repeat(32)} is ${status}`, structured = true) =>
  Object.assign(new Error('The design cannot be restored while a plan is open'), {
    status: 409,
    detail: {
      error: {
        code: 'plan_open',
        message: 'The design cannot be restored while a plan is open',
        details: {
          what: 'The design cannot be restored while a plan is open',
          why,
          suggestion: 'Use Roll back to... on the plan card, or abort the plan first.',
          ...(structured ? { code: 'plan_open', details: { planId: 'p'.repeat(32), status } } : {}),
        },
      },
    },
  });

describe('restoreSnapshot', () => {
  beforeEach(() => apiPost.mockReset().mockResolvedValue({ id: 'x', files: [] }));

  it('encodes the project and snapshot ids in the URL', async () => {
    await restoreSnapshot('a/b', '../c?d');
    expect(apiPost).toHaveBeenCalledWith('/projects/a%2Fb/history/..%2Fc%3Fd/restore', {});
  });
});

describe('planOpenRestoreHint', () => {
  it('reads the structured status first, the why text only as a fallback', () => {
    expect(planOpenRestoreHint(planOpen('paused', 'reworded text'))).toContain('Roll back to');
    expect(planOpenRestoreHint(planOpen('approved', `Plan x is approved`, false))).toContain('wait for it to run');
  });

  it('an approved plan: abort it or wait for it to run (no roll back, which needs a paused plan)', () => {
    const hint = planOpenRestoreHint(planOpen('approved'));
    expect(hint).toMatch(/approved/);
    expect(hint).toMatch(/Abort/);
    expect(hint).toMatch(/wait for it to run/);
    expect(hint).not.toMatch(/Roll back to…/);
    expect(hint).not.toMatch(/Stop/);
  });

  it('a running plan: stop it first', () => {
    const hint = planOpenRestoreHint(planOpen('running'));
    expect(hint).toMatch(/running/);
    expect(hint).toMatch(/Stop it/);
    expect(hint).toMatch(/Roll back to…/);
  });

  it('a paused plan: roll back or abort', () => {
    const hint = planOpenRestoreHint(planOpen('paused'));
    expect(hint).toMatch(/paused/);
    expect(hint).toMatch(/Roll back to…/);
    expect(hint).not.toMatch(/Stop/);
  });

  it('without a known status it covers every open state, including approved', () => {
    const hint = planOpenRestoreHint(new Error('plan open'));
    expect(hint).toMatch(/approved/);
    expect(hint).toMatch(/wait for it to run/);
    expect(hint).toMatch(/Roll back to…/);
  });
});
