import type { ConversationDTO } from '@/protocol/frames';

/** A ConversationDTO for tests: "Session <seq>", free, open, created at minute <seq>. */
export function conv(id: string, seq: number, extra: Partial<ConversationDTO> = {}): ConversationDTO {
  return {
    id,
    seq,
    title: `Session ${seq}`,
    type: 'free',
    status: 'open',
    archived: false,
    createdAt: `2026-10-19T10:${String(seq).padStart(2, '0')}:00.000Z`,
    lastMessageAt: null,
    closedAt: null,
    usage: { inputTokens: 0, outputTokens: 0, costUsd: 0 },
    ...extra,
  };
}
