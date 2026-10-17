import { z } from 'zod';
import { apiGet, apiPut, apiUrl } from '@/api/http';

export const PartInfo = z.object({
  mpn: z.string(),
  hasNotes: z.boolean(),
  hasDatasheet: z.boolean(),
  userEdited: z.boolean(),
  updatedAt: z.string().nullable(),
});
export type PartInfo = z.infer<typeof PartInfo>;

export const PartNotes = z.object({ mpn: z.string(), content: z.string(), userEdited: z.boolean() });
export type PartNotes = z.infer<typeof PartNotes>;

const enc = encodeURIComponent;
const notesPath = (projectId: string, mpn: string) => `/projects/${enc(projectId)}/parts/${enc(mpn)}/notes`;

export async function listParts(projectId: string): Promise<PartInfo[]> {
  return z.array(PartInfo).parse(await apiGet(`/projects/${enc(projectId)}/parts`));
}

export async function getNotes(projectId: string, mpn: string): Promise<PartNotes> {
  return PartNotes.parse(await apiGet(notesPath(projectId, mpn)));
}

/** The server marks saved notes `user_edited: true` so the researcher will not overwrite them. */
export const saveNotes = (projectId: string, mpn: string, content: string): Promise<void> =>
  apiPut(notesPath(projectId, mpn), { content });

/** Full URL (with /api) for a plain link. */
export const datasheetUrl = (projectId: string, mpn: string): string =>
  apiUrl(`/projects/${enc(projectId)}/parts/${enc(mpn)}/datasheet`);

/** Drops a leading YAML front matter block (`---\n…\n---`). */
export function stripFrontMatter(text: string): string {
  const m = /^---\r?\n[\s\S]*?\r?\n---[ \t]*(?:\r?\n|$)/.exec(text);
  return m ? text.slice(m[0].length) : text;
}
