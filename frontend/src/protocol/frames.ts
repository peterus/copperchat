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

export const AttachmentRef = z.strictObject({
  id: z.string().min(1).max(100),
  kind: z.enum(['image', 'pdf']),
  name: z.string().min(1).max(255),
});
export type AttachmentRef = z.infer<typeof AttachmentRef>;

export const MessageDTO = z.strictObject({
  id: z.number().int(),
  projectId: ProjectId,
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
  z.strictObject({ type: z.literal('subscribe'), projectId: ProjectId, sinceMessageId: z.number().int().nonnegative() }),
  z.strictObject({
    type: z.literal('send'),
    projectId: ProjectId,
    clientMessageId: z.string().min(1).max(100),
    text: z.string().min(1).max(100_000),
    attachments: z.array(AttachmentRef).max(20),
  }),
  z.strictObject({ type: z.literal('interrupt'), projectId: ProjectId, runId: z.string().nullable() }),
  z.strictObject({ type: z.literal('cancel-queued'), projectId: ProjectId, messageId: z.number().int().min(1) }),
  z.strictObject({ type: z.literal('ping'), projectId: NullProject }),
  // Page in the foreground or not (push goes out only while no client is visible, Phase 9).
  z.strictObject({ type: z.literal('visibility'), projectId: ProjectId, visible: z.boolean() }),
]);
export type ClientFrame = z.infer<typeof ClientFrame>;

export const ServerFrame = z.discriminatedUnion('type', [
  z.strictObject({ type: z.literal('message'), projectId: ProjectId, message: MessageDTO }),
  z
    .strictObject({
      type: z.literal('message-delta'),
      projectId: ProjectId,
      messageId: z.number().int(),
      contentDelta: z.string().optional(),
      statusPatch: MessageStatus.optional(),
      metadataPatch: z.record(z.string(), z.unknown()).optional(),
    })
    .refine((f) => f.contentDelta !== undefined || f.statusPatch !== undefined || f.metadataPatch !== undefined, {
      message: 'message-delta needs contentDelta, statusPatch or metadataPatch',
    }),
  z.strictObject({ type: z.literal('run-started'), projectId: ProjectId, runId: z.string() }),
  z.strictObject({ type: z.literal('run-finished'), projectId: ProjectId, runId: z.string(), reason: RunFinishReason }),
  z.strictObject({
    type: z.literal('view-updated'),
    projectId: ProjectId,
    docs: z.array(DocKind).min(1),
    version: z.number().int().nonnegative(),
  }),
  z.strictObject({ type: z.literal('usage'), projectId: ProjectId, usage: Usage }),
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
  'usage', 'rate-limited', 'error', 'pong',
] as const;

export const MessagesPage = z.object({ messages: z.array(MessageDTO), hasMore: z.boolean() });
export type MessagesPage = z.infer<typeof MessagesPage>;
