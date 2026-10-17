import { apiUrl, reportUnauthorized } from '@/api/http';
import { AttachmentRef } from '@/protocol/frames';

/** Multipart upload (apiPost JSON-encodes, so this uses fetch directly). */
export async function uploadAttachment(projectId: string, file: File): Promise<AttachmentRef> {
  const body = new FormData();
  body.append('file', file, file.name);
  const res = await fetch(apiUrl(`/projects/${encodeURIComponent(projectId)}/uploads`), {
    method: 'POST',
    body,
    credentials: 'same-origin',
  });
  if (res.status === 401) reportUnauthorized();
  const json: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = json && typeof json === 'object' ? (json as { detail?: unknown }).detail : undefined;
    throw new Error(typeof detail === 'string' ? detail : `Upload failed (${res.status})`);
  }
  const parsed = AttachmentRef.safeParse(json);
  if (!parsed.success) throw new Error('Upload failed (invalid server response)');
  return parsed.data;
}

/** Upload ids are 32 hex chars; anything else is never put into a URL. */
export const isUploadId = (id: string): boolean => /^[0-9a-f]{32}$/.test(id);

export const uploadUrl = (projectId: string, id: string): string =>
  apiUrl(`/projects/${encodeURIComponent(projectId)}/uploads/${id}`);
