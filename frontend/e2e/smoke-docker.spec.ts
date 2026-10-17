// Runs against the real app image (CI job `e2e`): DEV_FAKE_AGENT=true, the ecc83 fixture in
// /projects, a real KiCad api-server. Locally:
//   E2E_BASE_URL=http://127.0.0.1:8091 E2E_PASSWORD=secret npx playwright test e2e/smoke-docker.spec.ts --project=desktop
import { expect, test } from '@playwright/test';

test.skip(!process.env.E2E_BASE_URL, 'set E2E_BASE_URL (CI e2e job)');

test('login → open project → send → fake reply → viewer loads; PWA + CSP intact', async ({ page }) => {
  const cspViolations: string[] = [];
  page.on('console', (m) => {
    if (/Content Security Policy|Refused to/i.test(m.text())) cspViolations.push(m.text());
  });

  await page.goto('/projects');
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel('Username').fill(process.env.E2E_USERNAME ?? 'admin');
  await page.getByLabel('Password').fill(process.env.E2E_PASSWORD ?? 'secret');
  await page.getByRole('button', { name: 'Sign in' }).click();

  const viewerReq = page.waitForResponse(
    (r) => /\/api\/projects\/[0-9a-f]{12}\/view\/files\/[^/]+\.kicad_sch/.test(r.url()) && r.status() === 200,
    { timeout: 90_000 },
  );
  await page.getByRole('link', { name: /ecc83/ }).first().click();
  await expect(page).toHaveURL(/\/p\/[0-9a-f]{12}$/);
  // attached, not visible: on phones the viewer stays mounted behind the Chat tab
  await expect(page.getByTestId('viewer-slot').first()).toBeAttached();

  const text = `hello ${Date.now()}`; // unique: reruns against the same instance keep history
  await page.getByRole('textbox', { name: 'Message' }).fill(text);
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByText(text, { exact: true })).toBeVisible();
  await expect(page.getByText(`You said: ${text}`)).toBeVisible({ timeout: 30_000 });
  await viewerReq;

  // Direct navigation to a client route (SPA fallback) and the PWA files.
  const projectUrl = page.url();
  await page.reload();
  await expect(page).toHaveURL(projectUrl);
  await expect(page.getByRole('textbox', { name: 'Message' })).toBeVisible();
  const manifest = await page.request.get('/manifest.webmanifest');
  expect((await manifest.json()).name).toBe('CopperChat');
  const sw = await page.evaluate(async () => {
    const reg = await navigator.serviceWorker.getRegistration('/');
    return reg?.active?.scriptURL ?? reg?.installing?.scriptURL ?? reg?.waiting?.scriptURL ?? null;
  });
  expect(sw).toMatch(/\/sw\.js$/);
  expect(cspViolations).toEqual([]);
});
