/**
 * Plan REST (E-C12, E8b as built). Every event returns the plan's `PlanDTO`; responses are
 * parsed with zod (the same schema as the `plan-updated` frame).
 */
import { z } from 'zod';
import { PlanDTO } from '@/protocol/frames';
import { ApiError, apiGet, apiPost } from './http';

const enc = encodeURIComponent;
const base = (projectId: string) => `/projects/${enc(projectId)}/plans`;

export type PlanEvent = 'approve' | 'reject' | 'resume' | 'abort';

export async function fetchOpenPlan(projectId: string): Promise<PlanDTO | null> {
  return PlanDTO.nullable().parse(await apiGet(`${base(projectId)}/open`));
}

export async function fetchPlan(projectId: string, planId: string): Promise<PlanDTO> {
  return PlanDTO.parse(await apiGet(`${base(projectId)}/${enc(planId)}`));
}

export async function postPlanEvent(projectId: string, planId: string, event: PlanEvent): Promise<PlanDTO> {
  return PlanDTO.parse(await apiPost(`${base(projectId)}/${enc(planId)}/${event}`, {}));
}

export async function postPlanRollback(projectId: string, planId: string, toStep: number): Promise<PlanDTO> {
  return PlanDTO.parse(await apiPost(`${base(projectId)}/${enc(planId)}/rollback`, { toStep }));
}

const ErrorBody = z.object({ error: z.object({ code: z.string() }) });

/** The backend's error code (`{error: {code, message}}`), e.g. `agent_busy`, else null. */
export function apiErrorCode(e: unknown): string | null {
  if (!(e instanceof ApiError)) return null;
  const parsed = ErrorBody.safeParse(e.detail);
  return parsed.success ? parsed.data.error.code : null;
}
