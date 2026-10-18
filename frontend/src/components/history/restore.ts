import { apiPost } from '@/api/http';

/** POST /projects/{id}/history/{snapshotId}/restore (Phase 7). Rejects with ApiError on 400/404/409/422/503. */
export function restoreSnapshot(projectId: string, snapshotId: string): Promise<{ id: string; files: string[] }> {
  return apiPost(`/projects/${projectId}/history/${snapshotId}/restore`, {});
}

/** A restore refused because a plan has begun (409 `plan_open`, E8b): where to go instead. */
export const PLAN_OPEN_RESTORE =
  'A plan is open: use “Roll back to…” on its plan card, or abort the plan first, then restore.';

export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
