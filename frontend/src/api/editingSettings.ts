/** Editing settings of a project (E-C12, E1): `GET|PUT /projects/{id}/settings/editing`. */
import { z } from 'zod';
import { apiGet, apiPut } from './http';

export const CHANGE_BUDGET_MIN = 1;
export const CHANGE_BUDGET_MAX = 500;

export const EditingSettings = z.object({
  planPolicy: z.enum(['always', 'large', 'never']),
  changeBudget: z.number().int(),
  annotationBlock: z.union([z.literal(100), z.literal(1000)]),
  showAgentView: z.boolean(),
});
export type EditingSettings = z.infer<typeof EditingSettings>;

const path = (projectId: string) => `/projects/${encodeURIComponent(projectId)}/settings/editing`;

export async function fetchEditingSettings(projectId: string): Promise<EditingSettings> {
  return EditingSettings.parse(await apiGet(path(projectId)));
}

/** Partial update: only the given fields are sent; the response is the merged settings. */
export async function putEditingSettings(projectId: string, patch: Partial<EditingSettings>): Promise<EditingSettings> {
  return EditingSettings.parse(await apiPut(path(projectId), patch));
}
