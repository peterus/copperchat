/**
 * Change log, diff and ERC results of editing turns (E-C12, E8a as built). Payloads are parsed
 * with zod: a shape mismatch is an error, not silently wrong UI.
 */
import { z } from 'zod';
import { apiGet } from './http';

const enc = encodeURIComponent;

export const ChangeRun = z.object({
  runId: z.string().min(1),
  stepIds: z.array(z.string()),
  count: z.number().int().nonnegative(),
  sideEffects: z.number().int().nonnegative(),
  erc: z.object({ errors: z.number().int().nonnegative(), warnings: z.number().int().nonnegative(), resultId: z.string() }).nullable(),
  snapshotId: z.string().nullable(),
  createdAt: z.string().nullable(),
  /** The conversation (session) of the run (C1); null for runs before sessions existed. */
  conversationId: z.string().nullable().optional(),
  conversationTitle: z.string().nullable().optional(),
});
export type ChangeRun = z.infer<typeof ChangeRun>;

export const ChangeEntry = z.object({
  id: z.number().int(),
  runId: z.string(),
  planStepId: z.string().nullable(),
  callSeq: z.number().int(),
  tool: z.string(),
  /** add | modify | delete */
  op: z.string(),
  kind: z.string(),
  uuid: z.string(),
  sheetPath: z.string(),
  ref: z.string().nullable(),
  net: z.string().nullable(),
  before: z.record(z.string(), z.unknown()).nullable(),
  after: z.record(z.string(), z.unknown()).nullable(),
  sideEffect: z.boolean(),
  createdAt: z.string(),
});
export type ChangeEntry = z.infer<typeof ChangeEntry>;

const BoxMm = z.object({ x: z.number(), y: z.number(), w: z.number(), h: z.number() });

export const DiffEntry = z.object({
  id: z.string(),
  kind: z.enum(['added', 'modified', 'removed']),
  type: z.string(),
  uuid: z.string(),
  ref: z.string().nullable(),
  boxMm: BoxMm.nullable(),
  properties: z.array(z.object({ name: z.string(), before: z.unknown().optional(), after: z.unknown().optional() })),
  sideEffect: z.boolean(),
});
export type DiffEntry = z.infer<typeof DiffEntry>;

export const ChangesDiff = z.object({
  snapshotId: z.string(),
  count: z.number().int().nonnegative(),
  /** sheet instance path (`/<root uuid>/…`, = KiCanvas page `sheet_path`) → entries */
  sheets: z.record(z.string(), z.array(DiffEntry)),
});
export type ChangesDiff = z.infer<typeof ChangesDiff>;

export const ErcViolation = z.object({
  id: z.string().optional(),
  type: z.string(),
  severity: z.string(),
  description: z.string(),
});
export type ErcViolation = z.infer<typeof ErcViolation>;

export const ErcResult = z.object({
  id: z.string(),
  runId: z.string(),
  planStepId: z.string().nullable(),
  newErrors: z.array(ErcViolation),
  newWarnings: z.array(ErcViolation),
  resolved: z.array(ErcViolation),
  /** violation id → the agent's reason for leaving it */
  justifications: z.record(z.string(), z.string()),
  createdAt: z.string(),
});
export type ErcResult = z.infer<typeof ErcResult>;

export async function fetchChangeRuns(projectId: string, limit = 20): Promise<ChangeRun[]> {
  return z.array(ChangeRun).parse(await apiGet(`/projects/${enc(projectId)}/changes/runs?limit=${limit}`));
}

export async function fetchChanges(projectId: string, runId: string, stepId?: string | null): Promise<ChangeEntry[]> {
  const q = `runId=${enc(runId)}${stepId ? `&stepId=${enc(stepId)}` : ''}`;
  return z.array(ChangeEntry).parse(await apiGet(`/projects/${enc(projectId)}/changes?${q}`));
}

export async function fetchChangesDiff(projectId: string, snapshotId: string): Promise<ChangesDiff> {
  return ChangesDiff.parse(await apiGet(`/projects/${enc(projectId)}/changes/diff?snapshotId=${enc(snapshotId)}`));
}

export async function fetchErcResult(projectId: string, resultId: string): Promise<ErcResult> {
  return ErcResult.parse(await apiGet(`/projects/${enc(projectId)}/erc-results/${enc(resultId)}`));
}
