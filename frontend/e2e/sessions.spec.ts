/**
 * Sessions (phase C2): create sessions, queue a message in one while another runs, replies land in
 * their own session, switch sessions through the list (popover on desktop, sheet on the phone).
 * API and WebSocket are mocked, like viewer.spec.ts; no fake agent and no timing involved.
 */
import { readFileSync } from 'node:fs';
import { expect, type Page, test, type WebSocketRoute } from '@playwright/test';

const PID = 'a1b2c3d4e5f6';
const VERSION = (JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')) as { version: string }).version;

type Conversation = {
  id: string;
  seq: number;
  title: string;
  type: string;
  status: string;
  archived: boolean;
  createdAt: string;
  lastMessageAt: string | null;
  closedAt: string | null;
  usage: { inputTokens: number; outputTokens: number; costUsd: number };
};
type Message = Record<string, unknown> & { id: number; conversationId: string };

/** The mocked server: sessions and messages, plus the open socket the test pushes frames on. */
class FakeServer {
  conversations: Conversation[] = [];
  messages: Message[] = [];
  socket: WebSocketRoute | null = null;
  sends: Array<{ conversationId: string; text: string }> = [];
  private nextId = 1;

  create(type: string, title?: string): Conversation {
    const seq = this.conversations.length + 1;
    const c: Conversation = {
      id: String(seq).repeat(32),
      seq,
      title: title ?? `Session ${seq}`,
      type,
      status: 'open',
      archived: false,
      createdAt: new Date(Date.UTC(2026, 9, 20, 9, seq)).toISOString(),
      lastMessageAt: null,
      closedAt: null,
      usage: { inputTokens: 0, outputTokens: 0, costUsd: 0 },
    };
    this.conversations.push(c);
    return c;
  }

  message(conversationId: string, role: string, content: string, extra: Record<string, unknown> = {}): Message {
    const m: Message = {
      id: this.nextId++,
      projectId: PID,
      conversationId,
      clientMessageId: null,
      runId: null,
      role,
      kind: 'text',
      content,
      status: 'done',
      metadata: {},
      createdAt: '2026-10-20T09:30:00Z',
      ...extra,
    };
    this.messages.push(m);
    return m;
  }

  /** Stores the row (or replaces it by id) and pushes it as a `message` frame. */
  push(m: Message): void {
    const i = this.messages.findIndex((x) => x.id === m.id);
    if (i >= 0) this.messages[i] = m;
    this.frame({ type: 'message', projectId: PID, message: m });
  }

  frame(f: Record<string, unknown>): void {
    if (!this.socket) throw new Error('no socket');
    this.socket.send(JSON.stringify(f));
  }
}

async function mockApi(page: Page, server: FakeServer) {
  // Any API call not mocked below is a 404, never Vite's index.html (not /src/api/*.ts modules).
  await page.route(/^https?:\/\/[^/]+\/api\//, (r) => r.fulfill({ status: 404, json: { detail: 'not mocked' } }));
  await page.route('**/api/health', (r) => r.fulfill({ json: { status: 'ok', version: VERSION, kicad: 'ok' } }));
  await page.route('**/api/auth/me', (r) => r.fulfill({ json: { username: 'admin' } }));
  await page.route('**/api/projects', (r) =>
    r.fulfill({ json: [{ id: PID, name: 'demo', relPath: 'demo/demo.kicad_pro', hasSch: true, hasPcb: false, locked: false }] }),
  );
  await page.route(`**/api/projects/${PID}/open`, (r) =>
    r.fulfill({ json: { id: PID, readOnly: false, lockFiles: [], kicadVersion: '10.99.0', viewFiles: { sch: [], pcb: [] } } }),
  );
  await page.route(`**/api/projects/${PID}/docs`, (r) =>
    r.fulfill({ json: { root: 'docs/design', docs: [], ignored: [] } }),
  );
  await page.route(new RegExp(`/api/projects/${PID}/conversations(\\?.*)?$`), async (r) => {
    if (r.request().method() === 'POST') {
      const body = r.request().postDataJSON() as { type: string; title?: string };
      await r.fulfill({ status: 201, json: server.create(body.type, body.title) });
      return;
    }
    const archived = new URL(r.request().url()).searchParams.get('archived') === '1';
    await r.fulfill({ json: server.conversations.filter((c) => c.archived === archived) });
  });
  await page.route(new RegExp(`/api/projects/${PID}/conversations/([0-9a-f]{32})/messages`), (r) => {
    const cid = /conversations\/([0-9a-f]{32})\/messages/.exec(r.request().url())![1];
    r.fulfill({ json: { messages: server.messages.filter((m) => m.conversationId === cid), hasMore: false } });
  });
  await page.routeWebSocket('**/api/ws', (ws) => {
    server.socket = ws;
    ws.onMessage((raw) => {
      const f = JSON.parse(String(raw)) as { type: string; conversationId?: string; clientMessageId?: string; text?: string };
      if (f.type === 'ping') ws.send(JSON.stringify({ type: 'pong', projectId: null }));
      if (f.type === 'send') {
        server.sends.push({ conversationId: f.conversationId!, text: f.text ?? '' });
        // Another session runs: the message waits (FIFO across sessions).
        server.push(server.message(f.conversationId!, 'user', f.text ?? '', { clientMessageId: f.clientMessageId, status: 'queued' }));
      }
    });
  });
}

test('sessions: create, queue behind another session, replies land in their own session, switch', async ({ page }, info) => {
  const server = new FakeServer();
  await mockApi(page, server);
  await page.goto(`/p/${PID}`);

  // A new project: no session yet, the dialog defaults to Requirements (no requirements.md).
  await page.getByRole('button', { name: 'Start a session' }).click();
  const dialog = page.getByRole('dialog', { name: 'New session' });
  await expect(dialog.getByRole('radio', { name: /^Requirements/ })).toBeChecked();
  await dialog.getByRole('button', { name: 'Start session' }).click();
  const [a] = server.conversations;
  await expect(page).toHaveURL(new RegExp(`/p/${PID}/s/${a!.id}$`));
  await expect(page.getByRole('heading', { name: 'Session 1' })).toBeVisible();
  await expect(page.getByRole('img', { name: 'Requirements session' }).first()).toBeVisible();

  // A second, Free session.
  await page.getByRole('button', { name: 'New session' }).click();
  await page.getByRole('dialog', { name: 'New session' }).getByText('Free', { exact: true }).click();
  await page.getByRole('dialog', { name: 'New session' }).getByRole('button', { name: 'Start session' }).click();
  const b = server.conversations[1]!;
  await expect(page).toHaveURL(new RegExp(`/p/${PID}/s/${b.id}$`));
  await expect(page.getByRole('heading', { name: 'Session 2' })).toBeVisible();

  // Session 1 runs; a message sent in session 2 is queued behind it.
  server.frame({ type: 'run-started', projectId: PID, conversationId: a!.id, runId: 'run-a' });
  await expect(page.getByText('Session 1 running')).toBeVisible();
  await page.getByRole('textbox', { name: 'Message' }).fill('check the LDO');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByRole('list', { name: 'Queued messages' })).toContainText('check the LDO');
  await expect(page.getByText('Waiting for Session 1')).toBeVisible();
  expect(server.sends).toEqual([{ conversationId: b.id, text: 'check the LDO' }]);

  // Session 1 replies and finishes; then the queued message of session 2 runs and gets its reply.
  server.push(server.message(a!.id, 'assistant', 'Reply in session one', { runId: 'run-a' }));
  server.frame({ type: 'run-finished', projectId: PID, conversationId: a!.id, runId: 'run-a', reason: 'completed' });
  await expect(page.getByText('Session 1 running')).toBeHidden();
  const queued = server.messages.find((m) => m.conversationId === b.id)!;
  server.frame({ type: 'run-started', projectId: PID, conversationId: b.id, runId: 'run-b' });
  server.push({ ...queued, status: 'done', runId: 'run-b' });
  server.push(server.message(b.id, 'assistant', 'Reply in session two', { runId: 'run-b' }));
  server.frame({ type: 'run-finished', projectId: PID, conversationId: b.id, runId: 'run-b', reason: 'completed' });

  const log = page.getByRole('log');
  await expect(log.getByText('Reply in session two')).toBeVisible();
  await expect(log.getByText('check the LDO')).toBeVisible();
  await expect(page.getByText('Reply in session one')).toHaveCount(0);

  // Switch through the session list: a popover on desktop, a full-height sheet on the phone.
  await page.getByRole('button', { name: 'Sessions', exact: true }).click();
  const list = page.getByTestId('session-list');
  await expect(list).toHaveAttribute('data-variant', info.project.name === 'phone' ? 'sheet' : 'popover');
  const row = list.getByRole('listitem').filter({ hasText: 'Session 1' });
  await expect(row.getByLabel('unread')).toBeVisible();
  await row.getByRole('link').click();
  await expect(page).toHaveURL(new RegExp(`/p/${PID}/s/${a!.id}$`));
  await expect(list).toBeHidden();
  await expect(page.getByRole('heading', { name: 'Session 1' })).toBeVisible();
  await expect(log.getByText('Reply in session one')).toBeVisible();
  await expect(page.getByText('Reply in session two')).toHaveCount(0);

  // /p/X goes to the newest open session.
  await page.goto(`/p/${PID}`);
  await expect(page).toHaveURL(new RegExp(`/p/${PID}/s/[0-9a-f]{32}$`));
  await page.screenshot({ path: info.outputPath('sessions.png') });
});
