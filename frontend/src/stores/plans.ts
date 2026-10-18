import { createContext, useContext } from 'react';
import { createStore, type StoreApi, useStore } from 'zustand';
import {
  apiErrorCode,
  fetchOpenPlan,
  fetchPlan,
  type PlanEvent,
  postPlanEvent,
  postPlanRollback,
} from '@/api/plans';
import { type WsClient, wsClient } from '@/api/ws';
import { errorText } from '@/components/history/restore';
import type { PlanDTO } from '@/protocol/frames';

export const OPEN_STATUSES: ReadonlySet<PlanDTO['status']> = new Set(['proposed', 'approved', 'running', 'paused']);

export type PlanAction = PlanEvent | 'rollback';

export interface PlanError {
  action: PlanAction | 'load';
  code: string | null;
  message: string;
}

export interface PlansDeps {
  fetchOpen: (projectId: string) => Promise<PlanDTO | null>;
  fetchPlan: (projectId: string, planId: string) => Promise<PlanDTO>;
  event: (projectId: string, planId: string, event: PlanEvent) => Promise<PlanDTO>;
  rollback: (projectId: string, planId: string, toStep: number) => Promise<PlanDTO>;
}

export interface PlansState {
  projectId: string | null;
  /** Every plan seen (card rows of older plans, the open plan, frames). */
  byId: Record<string, PlanDTO>;
  /** The project's open plan (proposed / approved / running / paused), if known. */
  open: PlanDTO | null;
  /** The action in flight per plan: its buttons are disabled meanwhile. */
  pending: Record<string, PlanAction>;
  /** The last failure per plan (shown inline in its card), cleared by the next action. */
  errors: Record<string, PlanError>;
  load: (projectId: string) => Promise<void>;
  /** Fetches a plan once (a card of a plan not loaded yet). */
  ensure: (projectId: string, planId: string) => Promise<void>;
  approve: (planId: string) => Promise<void>;
  reject: (planId: string) => Promise<void>;
  resume: (planId: string) => Promise<void>;
  abort: (planId: string) => Promise<void>;
  rollback: (planId: string, toStep: number) => Promise<void>;
  dispose: () => void;
}

const DEFAULT_DEPS: PlansDeps = {
  fetchOpen: fetchOpenPlan,
  fetchPlan,
  event: postPlanEvent,
  rollback: postPlanRollback,
};

const ERROR_TEXT: Record<string, string> = {
  agent_busy: 'The agent is working. Wait until it has finished (or stop it), then try again.',
};

/**
 * Plans of the open project (E-C13). REST responses and `plan-updated` frames both upsert the
 * plan; an older copy (smaller `updatedAt`) never replaces a newer one. On a reconnect the
 * open and the loaded unfinished plans are refetched (frames may have been missed).
 */
export function createPlansStore(
  ws: Pick<WsClient, 'onFrame' | 'onConnection'>,
  deps: PlansDeps = DEFAULT_DEPS,
): StoreApi<PlansState> {
  // Bumped per project switch: responses of an older project are dropped.
  let generation = 0;
  const inflight = new Map<string, Promise<void>>();
  let openLoad: { projectId: string; promise: Promise<boolean> } | null = null;

  const upsert = (plan: PlanDTO) =>
    store.setState((s) => {
      const prev = s.byId[plan.id];
      if (prev && prev.updatedAt > plan.updatedAt) return s;
      const isOpen = OPEN_STATUSES.has(plan.status);
      const open = isOpen ? plan : s.open?.id === plan.id ? null : s.open;
      return { byId: { ...s.byId, [plan.id]: plan }, open };
    });

  const setError = (planId: string, error: PlanError | null) =>
    store.setState((s) => {
      const errors = { ...s.errors };
      if (error) errors[planId] = error;
      else delete errors[planId];
      return { errors };
    });

  /** One `GET /plans/open` at a time per project (cards mounting together share it). */
  /** Resolves true when the open plan (or none) was loaded. */
  const loadOpen = (projectId: string): Promise<boolean> => {
    if (openLoad?.projectId === projectId) return openLoad.promise;
    const promise = fetchOpen(projectId).finally(() => {
      if (openLoad?.promise === promise) openLoad = null;
    });
    openLoad = { projectId, promise };
    return promise;
  };
  const fetchOpen = async (projectId: string): Promise<boolean> => {
    const gen = generation;
    try {
      const plan = await deps.fetchOpen(projectId);
      if (gen !== generation) return false;
      if (plan) upsert(plan);
      else store.setState((s) => (s.open ? { open: null } : s));
      return true;
    } catch {
      // The cards of loaded plans still work; the next load or reconnect retries.
      return false;
    }
  };

  const fetchOne = (projectId: string, planId: string): Promise<void> => {
    const known = inflight.get(planId);
    if (known) return known;
    const gen = generation;
    const p = deps
      .fetchPlan(projectId, planId)
      .then(
        (plan) => {
          if (gen !== generation) return;
          upsert(plan);
          if (store.getState().errors[planId]?.action === 'load') setError(planId, null);
        },
        (e: unknown) => {
          if (gen === generation) setError(planId, { action: 'load', code: apiErrorCode(e), message: `Could not load the plan: ${errorText(e)}` });
        },
      )
      .finally(() => inflight.delete(planId));
    inflight.set(planId, p);
    return p;
  };

  const act = async (planId: string, action: PlanAction, run: (projectId: string) => Promise<PlanDTO>) => {
    const { projectId, pending } = store.getState();
    if (!projectId || pending[planId]) return; // one action per plan at a time (no double posts)
    const gen = generation;
    store.setState((s) => ({ pending: { ...s.pending, [planId]: action } }));
    setError(planId, null);
    try {
      const plan = await run(projectId);
      if (gen === generation) upsert(plan);
    } catch (e) {
      if (gen !== generation) return;
      const code = apiErrorCode(e);
      setError(planId, { action, code, message: (code && ERROR_TEXT[code]) || errorText(e) });
      // A refusal (409 plan_state) usually means the card is stale: show the current state.
      if (code === 'plan_state') void fetchOne(projectId, planId);
    } finally {
      if (gen === generation) {
        store.setState((s) => {
          const next = { ...s.pending };
          delete next[planId];
          return { pending: next };
        });
      }
    }
  };
  const event = (planId: string, e: PlanEvent) => act(planId, e, (pid) => deps.event(pid, planId, e));

  const store = createStore<PlansState>()(() => ({
    projectId: null,
    byId: {},
    open: null,
    pending: {},
    errors: {},
    load: async (projectId) => {
      if (projectId !== store.getState().projectId) {
        generation += 1;
        inflight.clear();
        openLoad = null;
        store.setState({ projectId, byId: {}, open: null, pending: {}, errors: {} });
      }
      await loadOpen(projectId);
    },
    ensure: async (projectId, planId) => {
      const s = store.getState();
      if (s.projectId !== projectId) await s.load(projectId);
      else if (openLoad?.projectId === projectId) await openLoad.promise;
      if (store.getState().byId[planId]) return;
      await fetchOne(projectId, planId);
    },
    approve: (planId) => event(planId, 'approve'),
    reject: (planId) => event(planId, 'reject'),
    resume: (planId) => event(planId, 'resume'),
    abort: (planId) => event(planId, 'abort'),
    rollback: (planId, toStep) => act(planId, 'rollback', (pid) => deps.rollback(pid, planId, toStep)),
    dispose: () => {
      offFrame();
      offConn();
    },
  }));

  const offFrame = ws.onFrame((f) => {
    if (f.type !== 'plan-updated') return;
    if (f.projectId !== store.getState().projectId || f.plan.projectId !== f.projectId) return;
    upsert(f.plan);
  });
  const offConn = ws.onConnection((state) => {
    if (state !== 'open' && state !== 'resumed') return;
    const { projectId, byId } = store.getState();
    if (!projectId) return;
    void loadOpen(projectId).then((loaded) => {
      // The open plan came with a successful loadOpen; the other unfinished ones are refetched.
      const { open, projectId: now } = store.getState();
      if (now !== projectId) return;
      for (const plan of Object.values(byId)) {
        if (OPEN_STATUSES.has(plan.status) && !(loaded && plan.id === open?.id)) void fetchOne(projectId, plan.id);
      }
    });
  });
  return store;
}

export const plansStore = createPlansStore(wsClient);
export const PlansStoreContext = createContext<StoreApi<PlansState> | null>(null);

export function usePlansStore(): StoreApi<PlansState> {
  return useContext(PlansStoreContext) ?? plansStore;
}

export function usePlans<T>(selector: (s: PlansState) => T): T {
  return useStore(usePlansStore(), selector);
}
