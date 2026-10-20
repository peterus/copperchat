/**
 * Design docs under docs/design/ (sessions S-C6/S-C8). C3 provides the index call; the
 * Docs panel (C4) adds the other routes. Payloads are parsed with zod: a shape mismatch is
 * an error, not silently wrong UI.
 */
import { z } from 'zod';
import { ApiError, apiGet, apiPost, apiPut } from './http';

const enc = encodeURIComponent;
/** `docs/design/notes/x.md` → each segment encoded, the slashes kept (the route takes a path). */
const docUrl = (projectId: string, path: string) =>
  `/projects/${enc(projectId)}/docs/${path.split('/').map(enc).join('/')}`;

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

/** `GET /docs/{path}` `meta` (plan S-C4 "As built (C3)"): approval is DB state, mirrored here. */
export const DocMeta = z.object({
  status: z.enum(['draft', 'approved']),
  userEdited: z.boolean(),
  approved: z.boolean(),
  approvedVersion: z.string().nullable(),
  approvedAt: z.string().nullable(),
  changedSinceApproval: z.boolean(),
});
export type DocMeta = z.infer<typeof DocMeta>;

/** A doc body without its (server-owned) front matter; `version` is null while it does not exist. */
export const DocRead = z.object({
  path: z.string(),
  exists: z.boolean(),
  version: z.string().nullable(),
  meta: DocMeta,
  body: z.string(),
  /** A missing fixed doc: the template its first write starts from. */
  template: z.string().optional(),
});
export type DocRead = z.infer<typeof DocRead>;

export const RevisionDTO = z.object({
  id: z.number().int(),
  version: z.string(),
  prevVersion: z.string().nullable(),
  /** agent | user | external */
  author: z.string(),
  conversationId: z.string().nullable(),
  /** The session label written at write time, e.g. "Session 2 — Schematic". */
  conversationTitle: z.string().nullable(),
  runId: z.string().nullable(),
  summary: z.string(),
  createdAt: z.string(),
  added: z.number().int().nonnegative(),
  removed: z.number().int().nonnegative(),
});
export type RevisionDTO = z.infer<typeof RevisionDTO>;

/** Whole file texts (front matter included); the client renders the diff. */
export const RevisionContent = z.object({ content: z.string(), prevContent: z.string().nullable() });
export type RevisionContent = z.infer<typeof RevisionContent>;

const VersionOut = z.object({ version: z.string() });
const version = async (p: Promise<unknown>) => VersionOut.parse(await p).version;

export async function getDoc(projectId: string, path: string): Promise<DocRead> {
  return DocRead.parse(await apiGet(docUrl(projectId, path)));
}

/** A user save (sets `user_edited`). `baseVersion` null creates the doc. 409 `doc_conflict`: see docConflictOf. */
export const putDoc = (projectId: string, path: string, body: string, baseVersion: string | null): Promise<string> =>
  version(apiPut(docUrl(projectId, path), { body, baseVersion }));

export const approveDoc = (projectId: string, path: string, v: string): Promise<string> =>
  version(apiPost(`${docUrl(projectId, path)}/approve`, { version: v }));

export const unapproveDoc = (projectId: string, path: string, v: string): Promise<string> =>
  version(apiPost(`${docUrl(projectId, path)}/unapprove`, { version: v }));

/** "Let the agent rewrite" (spec §7.3). */
export const clearUserEdited = (projectId: string, path: string, v: string): Promise<string> =>
  version(apiPost(`${docUrl(projectId, path)}/clear-user-edited`, { version: v }));

export async function listRevisions(projectId: string, path: string, limit = 50): Promise<RevisionDTO[]> {
  return z.array(RevisionDTO).parse(await apiGet(`${docUrl(projectId, path)}/revisions?limit=${limit}`));
}

export async function getRevision(projectId: string, path: string, rid: number): Promise<RevisionContent> {
  return RevisionContent.parse(await apiGet(`${docUrl(projectId, path)}/revisions/${rid}`));
}

/** A user write of that revision's body on top of `baseVersion`. */
export const revertDoc = (projectId: string, path: string, rid: number, baseVersion: string | null): Promise<string> =>
  version(apiPost(`${docUrl(projectId, path)}/revisions/${rid}/revert`, { baseVersion }));

/** Who changed the doc since the base version (409 `doc_conflict` details). */
export interface DocConflict {
  /** null: the doc no longer exists. */
  currentVersion: string | null;
  author: string | null;
  conversationTitle: string | null;
}

const ErrorBody = z.object({
  error: z.object({ code: z.string(), details: z.record(z.string(), z.unknown()).nullish() }),
});

/** The doc error code of a failed call (`doc_conflict`, `doc_too_large`, …), else null. */
export function docErrorCode(e: unknown): string | null {
  if (!(e instanceof ApiError)) return null;
  const parsed = ErrorBody.safeParse(e.detail);
  return parsed.success ? parsed.data.error.code : null;
}

const str = (v: unknown) => (typeof v === 'string' ? v : null);

export function docConflictOf(e: unknown): DocConflict | null {
  if (!(e instanceof ApiError) || e.status !== 409) return null;
  const parsed = ErrorBody.safeParse(e.detail);
  if (!parsed.success || parsed.data.error.code !== 'doc_conflict') return null;
  const d = parsed.data.error.details ?? {};
  return { currentVersion: str(d.currentVersion), author: str(d.author), conversationTitle: str(d.conversationTitle) };
}
