import { readFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const PID = 'a1b2c3d4e5f6';
const CID = '0123456789abcdef0123456789abcdef';
const REQ = 'docs/design/requirements.md';
const CONVERSATION = {
  id: CID, seq: 1, title: 'Session 1', type: 'free', status: 'open', archived: false,
  createdAt: '2026-10-19T10:00:00.000Z', lastMessageAt: null, closedAt: null,
  usage: { inputTokens: 0, outputTokens: 0, costUsd: 0 },
};
const VERSION = (JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')) as { version: string }).version;

// Phone width on both Playwright projects (spec §12.2 "Mobile").
test.use({ viewport: { width: 393, height: 851 } });

test('mocked API, phone: Docs under More → edit requirements.md → save → approve → history diff', async ({ page }) => {
  const doc = {
    body: '# Requirements\n\n## Power\n\nUSB-C, 5 V.\n',
    version: 'v1000000000000001',
    userEdited: false,
    approved: false,
    approvedVersion: null as string | null,
  };
  const revisions: Array<{ id: number; content: string; prevContent: string | null; version: string }> = [
    { id: 1, content: `---\ndoc: requirements\n---\n${doc.body}`, prevContent: null, version: doc.version },
  ];
  const puts: unknown[] = [];
  const approvals: unknown[] = [];
  const meta = () => ({
    status: doc.approved ? 'approved' : 'draft', userEdited: doc.userEdited, approved: doc.approved,
    approvedVersion: doc.approvedVersion, approvedAt: doc.approvedVersion ? '2026-10-19T12:00:00Z' : null,
    changedSinceApproval: doc.approvedVersion !== null && !doc.approved,
  });
  const entry = (path: string, exists: boolean) => ({
    path, fixed: true, exists, approved: exists && doc.approved, changedSinceApproval: false, approvedVersion: null,
    approvedAt: null, userEdited: exists && doc.userEdited, version: exists ? doc.version : null, bytes: 40,
    updatedAt: null, headings: ['Power'], openPoints: 0, lastAuthor: exists ? 'agent' : null, lastConversationTitle: null,
  });

  await page.route('**/api/health', (r) => r.fulfill({ json: { status: 'ok', version: VERSION, kicad: 'ok' } }));
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
  await page.route(`**/api/projects/${PID}/docs**`, async (r) => {
    const url = new URL(r.request().url());
    const path = decodeURIComponent(url.pathname.replace(`/api/projects/${PID}/docs`, '').replace(/^\//, ''));
    const method = r.request().method();
    if (path === '') {
      return r.fulfill({
        json: { root: 'docs/design', docs: [entry(REQ, true), entry('docs/design/architecture.md', false)], ignored: [] },
      });
    }
    if (method === 'GET' && path === REQ) {
      return r.fulfill({ json: { path: REQ, exists: true, version: doc.version, meta: meta(), body: doc.body } });
    }
    if (method === 'PUT' && path === REQ) {
      const body = r.request().postDataJSON() as { body: string; baseVersion: string };
      puts.push(body);
      const prev = doc.body;
      doc.body = body.body;
      doc.version = 'v2000000000000002';
      doc.userEdited = true;
      doc.approved = false;
      revisions.push({ id: 2, content: `---\ndoc: requirements\n---\n${doc.body}`, prevContent: `---\ndoc: requirements\n---\n${prev}`, version: doc.version });
      return r.fulfill({ json: { version: doc.version } });
    }
    if (method === 'POST' && path === `${REQ}/approve`) {
      approvals.push(r.request().postDataJSON());
      doc.approved = true;
      doc.approvedVersion = doc.version;
      return r.fulfill({ json: { version: doc.version } });
    }
    if (method === 'GET' && path === `${REQ}/revisions`) {
      return r.fulfill({
        json: [...revisions].reverse().map((v) => ({
          id: v.id, version: v.version, prevVersion: null, author: v.id === 1 ? 'agent' : 'user', conversationId: null,
          conversationTitle: v.id === 1 ? 'Session 1 — Requirements' : null, runId: null, summary: v.id === 1 ? 'created' : '',
          createdAt: '2026-10-19T12:00:00Z', added: v.id === 1 ? 5 : 1, removed: v.id === 1 ? 0 : 1,
        })),
      });
    }
    const rev = /^docs\/design\/requirements\.md\/revisions\/(\d+)$/.exec(path);
    if (method === 'GET' && rev) {
      const v = revisions.find((x) => x.id === Number(rev[1]))!;
      return r.fulfill({ json: { content: v.content, prevContent: v.prevContent } });
    }
    return r.fulfill({ status: 404, json: { error: { code: 'doc_not_found', message: 'not found', details: {} } } });
  });
  await page.routeWebSocket('**/api/ws', (ws) => {
    ws.onMessage((raw) => {
      const f = JSON.parse(String(raw)) as { type: string };
      if (f.type === 'ping') ws.send(JSON.stringify({ type: 'pong', projectId: null }));
    });
  });

  await page.goto('/projects');
  await page.getByRole('link', { name: /demo/ }).click();
  await page.getByRole('tab', { name: 'More' }).click();
  await page.getByRole('button', { name: 'Docs' }).click();
  await expect(page.getByText('architecture.md')).toBeVisible();
  await expect(page.getByText('missing')).toBeVisible();
  await page.getByText('requirements.md').click();
  await expect(page.getByRole('heading', { name: 'Power' })).toBeVisible();

  // edit full-screen, save
  await page.getByRole('button', { name: 'Edit' }).click();
  const box = page.getByRole('textbox', { name: 'Doc text' });
  await expect(box).toHaveValue(doc.body);
  const [boxSize, viewport] = [await box.boundingBox(), page.viewportSize()!];
  expect(boxSize!.width).toBeGreaterThan(viewport.width * 0.9);
  await box.fill('# Requirements\n\n## Power\n\nUSB-C PD, 9 V.\n');
  await page.getByRole('button', { name: 'Save' }).click();
  await expect(page.getByText('USB-C PD, 9 V.')).toBeVisible();
  expect(puts).toEqual([{ body: '# Requirements\n\n## Power\n\nUSB-C PD, 9 V.\n', baseVersion: 'v1000000000000001' }]);
  await expect(page.getByText('edited by you')).toBeVisible();

  // approve
  await page.getByRole('button', { name: 'Approve', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Unapprove' })).toBeVisible();
  expect(approvals).toEqual([{ version: 'v2000000000000002' }]);

  // history → the diff of the user's save
  await page.getByRole('button', { name: 'History' }).click();
  await page.getByRole('listitem').first().getByRole('button').click();
  const diff = page.getByRole('table', { name: 'Diff' });
  await expect(diff.locator('[data-op=del]')).toHaveText(['- USB-C, 5 V.']);
  await expect(diff.locator('[data-op=add]')).toHaveText(['+ USB-C PD, 9 V.']);
  const [d, vp] = [await diff.boundingBox(), page.viewportSize()!];
  expect(d!.width).toBeLessThanOrEqual(vp.width);
  await expect(page.locator('body')).not.toContainText('doc: requirements');
});
