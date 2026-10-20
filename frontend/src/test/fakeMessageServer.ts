import { vi } from 'vitest';
import type { MessageDTO } from '@/protocol/frames';

/** Default test project id (backend ids are 12 lowercase hex characters). */
export const PID = 'a1b2c3d4e5f6';
/** Default test conversation id (32 lowercase hex characters). */
export const CID = '0123456789abcdef0123456789abcdef';

export function row(id: number, overrides: Partial<MessageDTO> = {}): MessageDTO {
  return {
    id,
    projectId: PID,
    conversationId: CID,
    clientMessageId: null,
    runId: null,
    role: 'assistant',
    kind: 'text',
    content: `m${id}`,
    status: 'done',
    metadata: {},
    createdAt: '2026-10-15T10:00:00Z',
    ...overrides,
  };
}

/**
 * Fake backend for GET /api/projects/{pid}/conversations/{cid}/messages, installed as global
 * fetch. Mirrors backend MessageRepo.page: the conversation's newest `limit` rows below
 * `before`, ascending by id.
 */
export class FakeMessageServer {
  rows: MessageDTO[] = [];
  calls: string[] = [];
  failNext = 0;
  private held: Array<() => void> | null = null;

  constructor(rows: MessageDTO[] = []) {
    this.rows = rows;
  }
  install(): this {
    vi.stubGlobal('fetch', vi.fn((input: string | URL) => this.handle(input)));
    return this;
  }
  /** Requests started from now on wait until release(). */
  hold(): void {
    this.held = [];
  }
  release(): void {
    const waiting = this.held ?? [];
    this.held = null;
    for (const resume of waiting) resume();
  }
  messageCalls(): string[] {
    return this.calls.filter((c) => c.includes('/messages'));
  }

  private async handle(input: string | URL): Promise<Response> {
    const url = new URL(String(input), 'http://test');
    this.calls.push(url.pathname + url.search);
    const held = this.held;
    if (held) await new Promise<void>((resume) => held.push(resume));
    if (this.failNext > 0) {
      this.failNext -= 1;
      return new Response(JSON.stringify({ detail: 'boom' }), { status: 500, headers: { 'content-type': 'application/json' } });
    }
    const match = url.pathname.match(/^\/api\/projects\/([^/]+)\/conversations\/([^/]+)\/messages$/);
    if (!match) return new Response(null, { status: 404 });
    const limit = Number(url.searchParams.get('limit') ?? '50');
    const before = url.searchParams.get('before');
    const eligible = this.rows
      .filter((r) => r.projectId === match[1] && r.conversationId === match[2] && (before === null || r.id < Number(before)))
      .sort((a, b) => a.id - b.id);
    const body = { messages: eligible.slice(-limit), hasMore: eligible.length > limit };
    return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
  }
}
