/**
 * Conversations (sessions) REST, spec §6.1 / S-C4. Responses are parsed with zod (the same
 * `ConversationDTO` schema as the `conversation-updated` frame).
 */
import { z } from 'zod';
import { ConversationDTO, MessagesPage, type ConversationType } from '@/protocol/frames';
import { apiGet, apiPatch, apiPost } from './http';

const enc = encodeURIComponent;
const base = (projectId: string) => `/projects/${enc(projectId)}/conversations`;
const one = (projectId: string, conversationId: string) => `${base(projectId)}/${enc(conversationId)}`;

/** Newest activity first; `archived: true` lists the archived ones instead. */
export async function listConversations(projectId: string, opts: { archived?: boolean } = {}): Promise<ConversationDTO[]> {
  const raw = await apiGet<unknown>(`${base(projectId)}?archived=${opts.archived ? 1 : 0}`);
  return z.array(ConversationDTO).parse(raw);
}

/** 201. `title` is 1–120 chars on one line; the server numbers untitled ones ("Session N"). */
export async function createConversation(
  projectId: string,
  body: { type: ConversationType; title?: string },
): Promise<ConversationDTO> {
  return ConversationDTO.parse(await apiPost(base(projectId), body));
}

export async function patchConversation(
  projectId: string,
  conversationId: string,
  body: { title?: string; archived?: boolean },
): Promise<ConversationDTO> {
  return ConversationDTO.parse(await apiPatch(one(projectId, conversationId), body));
}

/** 202 (`closing`): enqueues the handover turn. 409 unless `open` (C5). */
export async function closeConversation(projectId: string, conversationId: string): Promise<ConversationDTO> {
  return ConversationDTO.parse(await apiPost(`${one(projectId, conversationId)}/close`, {}));
}

/** `closed` → `open` (C5). */
export async function reopenConversation(projectId: string, conversationId: string): Promise<ConversationDTO> {
  return ConversationDTO.parse(await apiPost(`${one(projectId, conversationId)}/reopen`, {}));
}

/** GET …/conversations/{cid}/messages: the newest `limit` rows below `before`, ascending by id. */
export async function fetchConversationMessages(
  projectId: string,
  conversationId: string,
  opts: { limit: number; before?: number },
): Promise<MessagesPage> {
  const q = new URLSearchParams({ limit: String(opts.limit) });
  if (opts.before !== undefined) q.set('before', String(opts.before));
  return MessagesPage.parse(await apiGet<unknown>(`${one(projectId, conversationId)}/messages?${q.toString()}`));
}
