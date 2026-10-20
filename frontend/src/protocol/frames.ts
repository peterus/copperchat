import { z } from 'zod';

/**
 * Zod mirror of the backend protocol (index C7). Keep in sync with
 * backend/src/copperchat/agent/protocol.py: same field constraints, unknown keys are
 * rejected (pydantic `extra="forbid"`), validated against protocol-fixtures/*.json.
 */
export const MessageRole = z.enum(['user', 'assistant', 'system']);
export const MessageKind = z.enum(['text', 'thinking', 'tool_use', 'tool_result', 'image', 'error', 'notice']);
export const MessageStatus = z.enum(['queued', 'streaming', 'done', 'cancelled', 'error']);
export type MessageStatus = z.infer<typeof MessageStatus>;
export const RunFinishReason = z.enum(['completed', 'interrupted', 'error', 'rate_limited']);
export const DocKind = z.enum(['sch', 'pcb']);
export type DocKind = z.infer<typeof DocKind>;

/** Backend project ids: 12 lowercase hex characters. */
export const ProjectId = z.string().regex(/^[0-9a-f]{12}$/);

/** Backend conversation (session) ids: 32 lowercase hex characters. */
export const ConversationId = z.string().regex(/^[0-9a-f]{32}$/);

export const ConversationType = z.enum(['free', 'requirements', 'schematic', 'board']);
export type ConversationType = z.infer<typeof ConversationType>;
export const ConversationStatus = z.enum(['open', 'closing', 'closed']);
export type ConversationStatus = z.infer<typeof ConversationStatus>;

/** S-C4 ConversationDTO (REST and the `conversation-updated` frame). */
export const ConversationDTO = z.strictObject({
  id: ConversationId,
  seq: z.number().int().min(1),
  title: z.string(),
  type: ConversationType,
  status: ConversationStatus,
  archived: z.boolean(),
  createdAt: z.string(),
  lastMessageAt: z.string().nullable(),
  closedAt: z.string().nullable(),
  usage: z.strictObject({
    inputTokens: z.number().int().nonnegative(),
    outputTokens: z.number().int().nonnegative(),
    costUsd: z.number(),
  }),
});
export type ConversationDTO = z.infer<typeof ConversationDTO>;

export const AttachmentRef = z.strictObject({
  id: z.string().min(1).max(100),
  kind: z.enum(['image', 'pdf']),
  name: z.string().min(1).max(255),
});
export type AttachmentRef = z.infer<typeof AttachmentRef>;

export const MessageDTO = z.strictObject({
  id: z.number().int(),
  projectId: ProjectId,
  conversationId: ConversationId,
  clientMessageId: z.string().nullable(),
  runId: z.string().nullable(),
  role: MessageRole,
  kind: MessageKind,
  content: z.string(),
  status: MessageStatus,
  metadata: z.record(z.string(), z.unknown()),
  createdAt: z.string(),
});
export type MessageDTO = z.infer<typeof MessageDTO>;

const TokenCount = z.number().int().nonnegative();

export const Usage = z.strictObject({
  inputTokens: TokenCount,
  outputTokens: TokenCount,
  cacheReadTokens: TokenCount,
  cacheCreationTokens: TokenCount,
  costUsd: z.number().optional(),
  rateLimit: z
    .strictObject({
      status: z.enum(['allowed', 'allowed_warning', 'rejected']),
      resetsAt: z.string().optional(),
    })
    .optional(),
});
export type Usage = z.infer<typeof Usage>;

const NullProject = z.null();

export const ClientFrame = z.discriminatedUnion('type', [
  // Replays that conversation; the subscription itself stays per project (spec §6.2).
  z.strictObject({
    type: z.literal('subscribe'),
    projectId: ProjectId,
    conversationId: ConversationId,
    sinceMessageId: z.number().int().nonnegative(),
  }),
  z.strictObject({
    type: z.literal('send'),
    projectId: ProjectId,
    conversationId: ConversationId,
    clientMessageId: z.string().min(1).max(100),
    text: z.string().min(1).max(100_000),
    attachments: z.array(AttachmentRef).max(20),
  }),
  z.strictObject({ type: z.literal('interrupt'), projectId: ProjectId, conversationId: ConversationId, runId: z.string().nullable() }),
  z.strictObject({ type: z.literal('cancel-queued'), projectId: ProjectId, messageId: z.number().int().min(1) }),
  z.strictObject({ type: z.literal('ping'), projectId: NullProject }),
  // Page in the foreground or not (push goes out only while no client is visible, Phase 9).
  z.strictObject({ type: z.literal('visibility'), projectId: ProjectId, visible: z.boolean() }),
]);
export type ClientFrame = z.infer<typeof ClientFrame>;

/** E-C11 PlanDTO (backend `PlanDTO` in agent/protocol.py). */
export const PlanStatus = z.enum(['proposed', 'approved', 'running', 'paused', 'done', 'rejected', 'aborted']);
export const PlanStepStatus = z.enum(['pending', 'running', 'done', 'failed', 'interrupted']);
export const PlanStepDTO = z.strictObject({
  idx: z.number().int().min(1),
  title: z.string(),
  intent: z.string(),
  expectedChanges: z.number().int().min(1),
  touches: z.strictObject({ refs: z.array(z.string()), nets: z.array(z.string()) }),
  verification: z.string(),
  status: PlanStepStatus,
  changeCount: z.number().int().nonnegative(),
  checkpointId: z.string().nullable(),
  summary: z.string().nullable(),
  failure: z.string().nullable(),
  erc: z
    .strictObject({
      errors: z.number().int().nonnegative(),
      warnings: z.number().int().nonnegative(),
      justifications: z.record(z.string(), z.string()),
      resultId: z.string().nullable(),
    })
    .nullable(),
});
export type PlanStepDTO = z.infer<typeof PlanStepDTO>;
export const PlanDTO = z.strictObject({
  id: z.string().min(1),
  projectId: ProjectId,
  conversationId: ConversationId,
  title: z.string(),
  summary: z.string(),
  status: PlanStatus,
  revision: z.number().int().min(1),
  pauseReason: z.string().nullable(),
  steps: z.array(PlanStepDTO),
  createdAt: z.string(),
  updatedAt: z.string(),
});
export type PlanDTO = z.infer<typeof PlanDTO>;

export const ServerFrame = z.discriminatedUnion('type', [
  z.strictObject({ type: z.literal('message'), projectId: ProjectId, message: MessageDTO }),
  z
    .strictObject({
      type: z.literal('message-delta'),
      projectId: ProjectId,
      conversationId: ConversationId,
      messageId: z.number().int(),
      contentDelta: z.string().optional(),
      statusPatch: MessageStatus.optional(),
      metadataPatch: z.record(z.string(), z.unknown()).optional(),
    })
    .refine((f) => f.contentDelta !== undefined || f.statusPatch !== undefined || f.metadataPatch !== undefined, {
      message: 'message-delta needs contentDelta, statusPatch or metadataPatch',
    }),
  z.strictObject({ type: z.literal('run-started'), projectId: ProjectId, conversationId: ConversationId, runId: z.string() }),
  z.strictObject({
    type: z.literal('run-finished'),
    projectId: ProjectId,
    conversationId: ConversationId,
    runId: z.string(),
    reason: RunFinishReason,
  }),
  z.strictObject({
    type: z.literal('view-updated'),
    projectId: ProjectId,
    docs: z.array(DocKind).min(1),
    version: z.number().int().nonnegative(),
  }),
  // Change rows of a run (and plan step) were written (E-C12).
  z.strictObject({
    type: z.literal('changes-updated'),
    projectId: ProjectId,
    runId: z.string().min(1),
    stepId: z.string().nullable(),
    count: z.number().int().nonnegative(),
  }),
  // A plan changed state (E-C12, E8b).
  z.strictObject({ type: z.literal('plan-updated'), projectId: ProjectId, plan: PlanDTO }),
  z.strictObject({ type: z.literal('usage'), projectId: ProjectId, conversationId: ConversationId, usage: Usage }),
  // A conversation (session) was created, renamed, archived, changed status or got its first/last message (C1).
  z.strictObject({ type: z.literal('conversation-updated'), projectId: ProjectId, conversation: ConversationDTO }),
  z.strictObject({ type: z.literal('rate-limited'), projectId: ProjectId, resetsAt: z.string() }),
  z.strictObject({
    type: z.literal('error'),
    // Errors about unparseable client frames cannot name a project; the server sends null then.
    projectId: ProjectId.nullable(),
    code: z.string().min(1),
    message: z.string(),
    clientMessageId: z.string().nullish(),
  }),
  z.strictObject({ type: z.literal('pong'), projectId: NullProject }),
  // Server heartbeat every 25 s; never answered (pong is server → client only).
  z.strictObject({ type: z.literal('ping'), projectId: NullProject }),
]);
export type ServerFrame = z.infer<typeof ServerFrame>;
export type MessageDeltaFrame = Extract<ServerFrame, { type: 'message-delta' }>;

export const AnyFrame = z.union([ClientFrame, ServerFrame]);

export const ALL_FRAME_TYPES = [
  'subscribe', 'send', 'interrupt', 'cancel-queued', 'ping', 'visibility',
  'message', 'message-delta', 'run-started', 'run-finished', 'view-updated',
  'changes-updated', 'plan-updated', 'usage', 'rate-limited', 'error', 'pong',
  'conversation-updated',
] as const;

export const MessagesPage = z.object({ messages: z.array(MessageDTO), hasMore: z.boolean() });
export type MessagesPage = z.infer<typeof MessagesPage>;
