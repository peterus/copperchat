import { apiPost } from '@/api/http';

/** POST /projects/{id}/history/{snapshotId}/restore (Phase 7). Rejects with ApiError on 400/404/409/422/503. */
export function restoreSnapshot(projectId: string, snapshotId: string): Promise<{ id: string; files: string[] }> {
  return apiPost(`/projects/${projectId}/history/${snapshotId}/restore`, {});
}

export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
