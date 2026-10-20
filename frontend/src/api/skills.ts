import { z } from 'zod';
import { apiGet, apiPost, apiPut } from '@/api/http';

export const SkillOrigin = z.enum(['builtin', 'user', 'project']);
export type SkillOrigin = z.infer<typeof SkillOrigin>;

export const SkillSummary = z.object({
  name: z.string(),
  origin: SkillOrigin,
  description: z.string(),
  shadowed: z.array(SkillOrigin),
  hasBuiltin: z.boolean(),
  originUpdated: z.boolean(),
  enabled: z.boolean(),
  /** Non-null (e.g. 'part-researcher') when the skill is loaded by a sub-agent, not the main agent. */
  agent: z.string().nullable(),
  /** Session types the skill is offered in (front matter `sessions:`); null = every type. */
  sessions: z.array(z.string()).nullable().default(null),
});
export type SkillSummary = z.infer<typeof SkillSummary>;

export const SkillDetail = SkillSummary.extend({
  content: z.string(),
  originDiff: z.string().nullable(),
  editable: z.boolean(),
});
export type SkillDetail = z.infer<typeof SkillDetail>;

const enc = encodeURIComponent;
const query = (projectId: string) => `projectId=${enc(projectId)}`;

export async function listSkills(projectId: string): Promise<SkillSummary[]> {
  return z.array(SkillSummary).parse(await apiGet(`/skills?${query(projectId)}`));
}

export async function getSkill(name: string, projectId: string): Promise<SkillDetail> {
  return SkillDetail.parse(await apiGet(`/skills/${enc(name)}?${query(projectId)}`));
}

export const customiseSkill = (name: string): Promise<unknown> => apiPost(`/skills/${enc(name)}/customise`, {});

export const saveUserSkill = (name: string, content: string): Promise<void> =>
  apiPut(`/skills/user/${enc(name)}`, { content });

export const resetSkill = (name: string): Promise<void> => apiPost(`/skills/${enc(name)}/reset`, {});

export const acknowledgeSkillUpdate = (name: string): Promise<void> =>
  apiPost(`/skills/${enc(name)}/acknowledge-update`, {});

export const setSkillEnabled = (projectId: string, name: string, enabled: boolean): Promise<void> =>
  apiPut(`/projects/${enc(projectId)}/skills/${enc(name)}`, { enabled });
