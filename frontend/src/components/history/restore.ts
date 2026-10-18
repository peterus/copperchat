import { apiPost } from '@/api/http';

const enc = encodeURIComponent;

/** POST /projects/{id}/history/{snapshotId}/restore (Phase 7). Rejects with ApiError on 400/404/409/422/503. */
export function restoreSnapshot(projectId: string, snapshotId: string): Promise<{ id: string; files: string[] }> {
  return apiPost(`/projects/${enc(projectId)}/history/${enc(snapshotId)}/restore`, {});
}

type OpenStatus = 'approved' | 'running' | 'paused';

/**
 * A restore refused because a plan has begun (409 `plan_open`, E8b): where to go instead, by the
 * plan's state. "Roll back to…" exists on the card only while paused, Abort not while running.
 */
const PLAN_OPEN_HINT: Record<OpenStatus | 'unknown', string> = {
  approved: 'An approved plan is waiting to run. Abort it on its plan card to restore now, or wait for it to run.',
  running: 'A plan is running. Stop it; then use “Roll back to…” on its plan card, or abort the plan and restore.',
  paused: 'A plan is paused. Use “Roll back to…” on its plan card, or abort the plan and restore.',
  unknown:
    'A plan is open. If it is approved, abort it or wait for it to run; if it is running, stop it. Once it is paused, use “Roll back to…” on its plan card, or abort the plan and restore.',
};

/** The open plan's state from the 409 body (`details.why` = "Plan <id> is <status>", routes_history.py). */
function openStatus(e: unknown): OpenStatus | null {
  const detail = e && typeof e === 'object' && 'detail' in e ? (e as { detail: unknown }).detail : null;
  const why = (detail as { error?: { details?: { why?: unknown } } } | null)?.error?.details?.why;
  const m = typeof why === 'string' ? /\bis (approved|running|paused)\b/.exec(why) : null;
  return m ? (m[1] as OpenStatus) : null;
}

export function planOpenRestoreHint(e: unknown): string {
  return PLAN_OPEN_HINT[openStatus(e) ?? 'unknown'];
}

export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
