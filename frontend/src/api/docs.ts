/**
 * Design docs under docs/design/ (sessions S-C6/S-C8). C3 provides the index call; the
 * Docs panel (C4) adds the other routes. Payloads are parsed with zod: a shape mismatch is
 * an error, not silently wrong UI.
 */
import { z } from 'zod';
import { apiGet } from './http';

const enc = encodeURIComponent;

export const DocIndexEntry = z.object({
  path: z.string(),
  fixed: z.boolean(),
  exists: z.boolean(),
  approved: z.boolean(),
  changedSinceApproval: z.boolean(),
  approvedVersion: z.string().nullable(),
  approvedAt: z.string().nullable(),
  userEdited: z.boolean(),
  version: z.string().nullable(),
  bytes: z.number().int().nonnegative(),
  updatedAt: z.string().nullable(),
  /** Level-2 headings (sanitised, one line each). */
  headings: z.array(z.string()),
  openPoints: z.number().int().nonnegative(),
  /** agent | user | external */
  lastAuthor: z.string().nullable(),
  lastConversationTitle: z.string().nullable(),
});
export type DocIndexEntry = z.infer<typeof DocIndexEntry>;

export const DocIndexDTO = z.object({
  root: z.string(),
  docs: z.array(DocIndexEntry),
  /** Files under docs/design/ that are not read: symlinks, special files, too large, … */
  ignored: z.array(z.object({ path: z.string(), reason: z.string() })),
});
export type DocIndexDTO = z.infer<typeof DocIndexDTO>;

export async function getDocIndex(projectId: string): Promise<DocIndexDTO> {
  return DocIndexDTO.parse(await apiGet(`/projects/${enc(projectId)}/docs`));
}
