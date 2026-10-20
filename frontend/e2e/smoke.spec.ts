import { readFileSync } from 'node:fs';
import { expect, type Page, test } from '@playwright/test';

const PID = 'a1b2c3d4e5f6';
const CID = '0123456789abcdef0123456789abcdef';
/** The project's one conversation (C1 interim default session). */
const CONVERSATION = {
  id: CID,
  seq: 1,
  title: 'Session 1',
  type: 'free',
  status: 'open',
  archived: false,
  createdAt: '2026-10-19T10:00:00.000Z',
  lastMessageAt: null,
  closedAt: null,
  usage: { inputTokens: 0, outputTokens: 0, costUsd: 0 },
};
const VERSION = (JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')) as { version: string }).version;
/** The build-version check on every WS connect (UpdateBanner). */
const mockHealth = (page: Page, version = VERSION) =>
  page.route('**/api/health', (r) => r.fulfill({ json: { status: 'ok', version, kicad: 'ok' } }));
const msg = (id: number, role: string, content: string, extra: Record<string, unknown> = {}) => ({
  id,
  projectId: PID,
  conversationId: CID,
  clientMessageId: null,
  runId: null,
  role,
  kind: 'text',
  content,
  status: 'done',
  metadata: {},
  createdAt: '2026-10-15T10:00:00Z',
  ...extra,
});

test('mocked API: login → pick project → chat round-trip', async ({ page }) => {
  await mockHealth(page);
  let authed = false;
  await page.route('**/api/auth/me', (r) =>
    authed ? r.fulfill({ json: { username: 'admin' } }) : r.fulfill({ status: 401, json: { detail: 'not authenticated' } }),
  );
  await page.route('**/api/auth/login', async (r) => {
    authed = true;
    await r.fulfill({ status: 204 });
  });
  await page.route('**/api/projects', (r) =>
    r.fulfill({
      json: [
        { id: PID, name: 'demo', relPath: 'demo/demo.kicad_pro', hasSch: true, hasPcb: true, locked: false },
        { id: 'ffffffffffff', name: 'busy', relPath: 'lab/busy.kicad_pro', hasSch: true, hasPcb: false, locked: true },
      ],
    }),
  );
  await page.route(`**/api/projects/${PID}/open`, (r) =>
    r.fulfill({
      json: {
        id: PID,
        readOnly: false,
        lockFiles: [],
        kicadVersion: '10.99.0',
        viewFiles: { sch: ['demo.kicad_sch'], pcb: ['demo.kicad_pcb'] },
      },
    }),
  );
  await page.route(new RegExp(`/api/projects/${PID}/conversations(\\?.*)?$`), (r) => r.fulfill({ json: [CONVERSATION] }));
  await page.route(`**/api/projects/${PID}/conversations/${CID}/messages**`, (r) =>
    r.fulfill({ json: { messages: [msg(1, 'assistant', 'Hello from the fixture')], hasMore: false } }),
  );
  const seen: string[] = [];
  await page.routeWebSocket('**/api/ws', (ws) => {
    ws.onMessage((raw) => {
      const f = JSON.parse(String(raw)) as { type: string; conversationId?: string; clientMessageId?: string; text?: string };
      if (f.type === 'ping') ws.send(JSON.stringify({ type: 'pong', projectId: null }));
      if (f.type === 'subscribe' || f.type === 'send') seen.push(`${f.type}:${f.conversationId}`);
      if (f.type === 'send') {
        ws.send(
          JSON.stringify({
            type: 'message',
            projectId: PID,
            message: msg(2, 'user', f.text ?? '', { clientMessageId: f.clientMessageId }),
          }),
        );
        ws.send(JSON.stringify({ type: 'run-started', projectId: PID, conversationId: CID, runId: 'r1' }));
        ws.send(
          JSON.stringify({ type: 'message', projectId: PID, message: msg(3, 'assistant', `Echo: ${f.text}`, { runId: 'r1' }) }),
        );
        ws.send(JSON.stringify({ type: 'run-finished', projectId: PID, conversationId: CID, runId: 'r1', reason: 'completed' }));
      }
    });
  });

  await page.goto('/projects');
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByText(/Powered by Claude/)).toBeVisible();
  await page.getByLabel('Username').fill('admin');
  await page.getByLabel('Password').fill('secret');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByText(/Open in KiCad — close it there/)).toBeVisible();
  await page.getByRole('link', { name: /demo/ }).click();
  await expect(page.getByText('Hello from the fixture')).toBeVisible();
  await page.getByRole('textbox', { name: 'Message' }).fill('ping the board');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByText('Echo: ping the board')).toBeVisible();
  // subscribe (replay) and send name the conversation
  expect(seen).toEqual(expect.arrayContaining([`subscribe:${CID}`, `send:${CID}`]));
  await expect(page.locator('body')).not.toContainText('Claude Code');
});

test('mocked API: an old tab after an upgrade offers a reload', async ({ page }) => {
  await mockHealth(page, '999.0.0');
  await page.route('**/api/auth/me', (r) => r.fulfill({ json: { username: 'admin' } }));
  await page.route('**/api/projects', (r) =>
    r.fulfill({ json: [{ id: PID, name: 'demo', relPath: 'demo/demo.kicad_pro', hasSch: true, hasPcb: false, locked: false }] }),
  );
  await page.route(`**/api/projects/${PID}/open`, (r) =>
    r.fulfill({ json: { id: PID, readOnly: false, lockFiles: [], kicadVersion: '10.99.0', viewFiles: { sch: ['demo.kicad_sch'], pcb: [] } } }),
  );
  await page.route(new RegExp(`/api/projects/${PID}/conversations(\\?.*)?$`), (r) => r.fulfill({ json: [CONVERSATION] }));
  await page.route(`**/api/projects/${PID}/conversations/${CID}/messages**`, (r) =>
    r.fulfill({ json: { messages: [], hasMore: false } }),
  );
  await page.routeWebSocket('**/api/ws', (ws) => {
    ws.onMessage((raw) => {
      if ((JSON.parse(String(raw)) as { type: string }).type === 'ping') ws.send(JSON.stringify({ type: 'pong', projectId: null }));
    });
  });
  await page.goto(`/p/${PID}`);
  const banner = page.getByRole('alert').filter({ hasText: 'CopperChat was updated — reload' });
  await expect(banner).toBeVisible();
  await expect(banner.getByRole('button', { name: 'Reload' })).toBeVisible();
});

test('real backend with dev_fake_agent (opt-in)', async ({ page }) => {
  test.skip(!process.env.E2E_BASE_URL || !process.env.E2E_PASSWORD, 'set E2E_BASE_URL, E2E_USERNAME, E2E_PASSWORD');
  await page.goto('/login');
  await page.getByLabel('Username').fill(process.env.E2E_USERNAME ?? 'admin');
  await page.getByLabel('Password').fill(process.env.E2E_PASSWORD ?? '');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await page.getByRole('link').filter({ hasText: '.kicad_pro' }).first().click();
  await page.getByRole('textbox', { name: 'Message' }).fill('hello');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.locator('[data-role="assistant"]').last()).toBeVisible({ timeout: 30_000 });
});

test('mocked API: a session cookie the browser dropped is explained, not looped', async ({ page }) => {
  let logins = 0;
  await page.route('**/api/auth/me', (r) => r.fulfill({ status: 401, json: { detail: 'not logged in' } }));
  await page.route('**/api/auth/login', async (r) => {
    logins += 1;
    await r.fulfill({ status: 204 });
  });
  await page.goto('/projects');
  await page.getByLabel('Username').fill('admin');
  await page.getByLabel('Password').fill('secret');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('alert')).toContainText('did not keep the session cookie');
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByLabel('Username')).toHaveValue('admin');
  expect(logins).toBe(1);
});

test('mocked API: plan card propose → approve → done in the chat', async ({ page }, info) => {
  const PLAN = 'f'.repeat(32);
  const step = (idx: number, status: string, over: Record<string, unknown> = {}) => ({
    idx, title: `Step ${idx}`, intent: '', expectedChanges: 2, touches: { refs: [], nets: [] }, verification: '',
    status, changeCount: 0, checkpointId: null, summary: null, failure: null, erc: null, ...over,
  });
  const plan = (status: string, steps: unknown[], updatedAt: string) => ({
    id: PLAN, projectId: PID, conversationId: CID, title: 'Add a status LED', summary: 'R and LED on GPIO2.', status, revision: 1,
    pauseReason: null, steps, createdAt: '2026-10-18T10:00:00.000Z', updatedAt,
  });
  let current = plan('proposed', [step(1, 'pending'), step(2, 'pending')], '2026-10-18T10:00:00.000Z');
  let approves = 0;
  await mockHealth(page);
  await page.route('**/api/auth/me', (r) => r.fulfill({ json: { username: 'admin' } }));
  await page.route('**/api/projects', (r) =>
    r.fulfill({ json: [{ id: PID, name: 'demo', relPath: 'demo/demo.kicad_pro', hasSch: true, hasPcb: false, locked: false }] }),
  );
  await page.route(`**/api/projects/${PID}/open`, (r) =>
    r.fulfill({ json: { id: PID, readOnly: false, lockFiles: [], kicadVersion: '10.99.0', viewFiles: { sch: ['demo.kicad_sch'], pcb: [] } } }),
  );
  await page.route(new RegExp(`/api/projects/${PID}/conversations(\\?.*)?$`), (r) => r.fulfill({ json: [CONVERSATION] }));
  await page.route(`**/api/projects/${PID}/conversations/${CID}/messages**`, (r) =>
    r.fulfill({
      json: {
        messages: [
          msg(1, 'user', 'add a status LED'),
          msg(2, 'system', 'The agent proposed a plan.', { kind: 'notice', metadata: { action: 'plan', planId: PLAN } }),
        ],
        hasMore: false,
      },
    }),
  );
  await page.route(`**/api/projects/${PID}/plans/**`, (r) => r.fulfill({ json: current }));
  let socket: { send: (s: string) => void } | null = null;
  await page.route(`**/api/projects/${PID}/plans/${PLAN}/approve`, async (r) => {
    approves += 1;
    current = plan('approved', [step(1, 'pending'), step(2, 'pending')], '2026-10-18T10:01:00.000Z');
    await r.fulfill({ json: current });
  });
  await page.routeWebSocket('**/api/ws', (ws) => {
    socket = ws;
    ws.onMessage((raw) => {
      const f = JSON.parse(String(raw)) as { type: string };
      if (f.type === 'ping') ws.send(JSON.stringify({ type: 'pong', projectId: null }));
    });
  });

  await page.goto('/projects');
  await page.getByRole('link', { name: /demo/ }).click();
  const card = page.getByRole('region', { name: 'Add a status LED' });
  await expect(card).toBeVisible();
  const approve = card.getByRole('button', { name: 'Approve' });
  await expect(approve).toBeVisible();
  if (info.project.name === 'phone') {
    // full-width buttons in the phone chat stream
    const [b, c] = await Promise.all([approve.boundingBox(), card.boundingBox()]);
    expect(b!.width).toBeGreaterThan(c!.width * 0.8);
  }
  await approve.click();
  await expect(card.getByText('Waiting for the agent to start.')).toBeVisible();
  expect(approves).toBe(1);

  const frame = (p: unknown) => socket!.send(JSON.stringify({ type: 'plan-updated', projectId: PID, plan: p }));
  frame(plan('running', [step(1, 'running'), step(2, 'pending')], '2026-10-18T10:02:00.000Z'));
  await expect(card.getByText('Running').first()).toBeVisible();
  frame(
    plan(
      'done',
      [
        step(1, 'done', { changeCount: 2, checkpointId: 'a'.repeat(32), erc: { errors: 0, warnings: 0, justifications: {}, resultId: null } }),
        step(2, 'done', { changeCount: 3, checkpointId: 'b'.repeat(32) }),
      ],
      '2026-10-18T10:03:00.000Z',
    ),
  );
  await expect(card.getByText('3 changes')).toBeVisible();
  await expect(card.getByRole('button', { name: 'Approve' })).toHaveCount(0);
  await expect(card.getByRole('button', { name: 'Stop' })).toHaveCount(0);
  await expect(card.getByRole('button', { name: 'Checkpoint' })).toHaveCount(2);
});
