import { expect, test } from '@playwright/test';

const PID = 'a1b2c3d4e5f6';
const msg = (id: number, role: string, content: string, extra: Record<string, unknown> = {}) => ({
  id,
  projectId: PID,
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
  await page.route(`**/api/projects/${PID}/messages**`, (r) =>
    r.fulfill({ json: { messages: [msg(1, 'assistant', 'Hello from the fixture')], hasMore: false } }),
  );
  await page.routeWebSocket('**/api/ws', (ws) => {
    ws.onMessage((raw) => {
      const f = JSON.parse(String(raw)) as { type: string; clientMessageId?: string; text?: string };
      if (f.type === 'ping') ws.send(JSON.stringify({ type: 'pong', projectId: null }));
      if (f.type === 'send') {
        ws.send(
          JSON.stringify({
            type: 'message',
            projectId: PID,
            message: msg(2, 'user', f.text ?? '', { clientMessageId: f.clientMessageId }),
          }),
        );
        ws.send(JSON.stringify({ type: 'run-started', projectId: PID, runId: 'r1' }));
        ws.send(
          JSON.stringify({ type: 'message', projectId: PID, message: msg(3, 'assistant', `Echo: ${f.text}`, { runId: 'r1' }) }),
        );
        ws.send(JSON.stringify({ type: 'run-finished', projectId: PID, runId: 'r1', reason: 'completed' }));
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
  await expect(page.locator('body')).not.toContainText('Claude Code');
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
