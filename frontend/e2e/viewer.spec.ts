/**
 * The real, vendored KiCanvas bundle (public/vendor/kicanvas, peterus/kicanvas fork) on the KiCad 11
 * fixture projects of the backend (backend/tests/fixtures/projects). The API is mocked: view
 * files are served from the fixtures, the agent view is a tiny PNG.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { expect, type Page, test } from '@playwright/test';

const PID = 'a1b2c3d4e5f6';
const FIXTURES = fileURLToPath(new URL('../../backend/tests/fixtures/projects/', import.meta.url));
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
);

interface Fixture {
  dir: string;
  sch: string[];
  pcb: string[];
  /** Override reported sizes (large-board guard). */
  bytes?: Record<string, number>;
}
const ECC83: Fixture = { dir: 'ecc83', sch: ['ecc83-pp.kicad_sch'], pcb: ['ecc83-pp.kicad_pcb'] };
const HIER: Fixture = {
  dir: 'complex_hierarchy',
  sch: ['complex_hierarchy.kicad_sch', 'ampli_ht.kicad_sch'],
  pcb: ['complex_hierarchy.kicad_pcb'],
};

async function mockApi(page: Page, fx: Fixture) {
  const file = (name: string) => readFileSync(`${FIXTURES}${fx.dir}/${name}`);
  const bytes = Object.fromEntries([...fx.sch, ...fx.pcb].map((n) => [n, fx.bytes?.[n] ?? file(n).length]));
  await page.route('**/api/auth/me', (r) => r.fulfill({ json: { username: 'admin' } }));
  await page.route('**/api/projects', (r) =>
    r.fulfill({ json: [{ id: PID, name: fx.dir, relPath: `${fx.dir}/x.kicad_pro`, hasSch: true, hasPcb: true, locked: false }] }),
  );
  await page.route(`**/api/projects/${PID}/open`, (r) =>
    r.fulfill({
      json: { id: PID, readOnly: false, lockFiles: [], kicadVersion: '10.99', viewFiles: { sch: fx.sch, pcb: fx.pcb }, viewBytes: bytes },
    }),
  );
  await page.route(`**/api/projects/${PID}/messages**`, (r) => r.fulfill({ json: { messages: [], hasMore: false } }));
  await page.route(`**/api/projects/${PID}/view/files/**`, (r) => {
    const name = decodeURIComponent(new URL(r.request().url()).pathname.split('/view/files/')[1] ?? '');
    return r.fulfill({ body: file(name), contentType: 'application/octet-stream' });
  });
  await page.route(`**/api/projects/${PID}/agent-view**`, (r) => r.fulfill({ body: PNG, contentType: 'image/png' }));
  await page.routeWebSocket('**/api/ws', (ws) => {
    ws.onMessage((raw) => {
      if (JSON.parse(String(raw)).type === 'ping') ws.send(JSON.stringify({ type: 'pong', projectId: null }));
    });
  });
}

/** The KiCanvas Viewer of the visible embed (test-only shadow-DOM walk). */
const VIEWER_HELPER = () => {
  (window as unknown as { __kcViewer: () => unknown }).__kcViewer = () => {
    const deep = (root: ParentNode, sel: string): Element | null => {
      const queue: ParentNode[] = [root];
      while (queue.length) {
        const r = queue.shift()!;
        const hit = r.querySelector(sel);
        if (hit) return hit;
        r.querySelectorAll('*').forEach((n) => n.shadowRoot && queue.push(n.shadowRoot));
      }
      return null;
    };
    const embed = Array.from(document.querySelectorAll('kicanvas-embed')).find((e) => (e as HTMLElement).offsetParent !== null);
    const el = embed?.shadowRoot && deep(embed.shadowRoot, 'kc-schematic-viewer, kc-board-viewer');
    return (el as unknown as { viewer?: unknown } | null)?.viewer ?? null;
  };
};

type AnyViewer = {
  selected: { center: unknown } | null;
  viewport: { camera: { zoom: number; world_to_screen(v: unknown): { x: number; y: number } } };
  canvas: HTMLCanvasElement;
};

/** Client coordinates of the centre of the viewer's current selection. */
async function selectionPoint(page: Page) {
  return page.evaluate(() => {
    const v = (window as unknown as { __kcViewer: () => AnyViewer }).__kcViewer();
    const sel = v.selected!;
    const p = v.viewport.camera.world_to_screen(sel.center);
    const r = v.canvas.getBoundingClientRect();
    return { x: r.left + p.x, y: r.top + p.y };
  });
}

const zoomOf = (page: Page) =>
  page.evaluate(() => (window as unknown as { __kcViewer: () => AnyViewer }).__kcViewer().viewport.camera.zoom);

async function openViewer(page: Page, phone: boolean) {
  await page.goto(`/p/${PID}`);
  if (phone) await page.getByRole('tab', { name: 'View' }).click();
}

const ready = (page: Page) => expect(page.getByRole('button', { name: 'Fit to view' }).first()).toBeVisible({ timeout: 20_000 });

test.beforeEach(async ({ page }) => {
  await page.addInitScript(VIEWER_HELPER);
});

test.describe('desktop', () => {
  test.skip(({ isMobile }) => isMobile, 'desktop only');

  test('schematic renders offline, Find selects without a mention, a click inserts @REF', async ({ page, baseURL }) => {
    const external: string[] = [];
    const errors: string[] = [];
    const parserWarnings: string[] = [];
    page.on('request', (r) => !r.url().startsWith(baseURL!) && !r.url().startsWith('data:') && external.push(r.url()));
    page.on('pageerror', (e) => errors.push(e.message));
    page.on('console', (m) => m.text().includes('No definition found') && parserWarnings.push(m.text()));
    await mockApi(page, ECC83);
    await openViewer(page, false);
    await ready(page);
    expect(await page.locator('kicanvas-embed').first().getAttribute('theme')).toMatch(/^(kicad|witchhazel)$/);
    await page.getByRole('textbox', { name: 'Find reference' }).fill('U1');
    await page.getByRole('button', { name: 'Find', exact: true }).click();
    await expect.poll(() => page.evaluate(() => Boolean((window as unknown as { __kcViewer: () => AnyViewer }).__kcViewer().selected))).toBe(true);
    await expect(page.getByRole('textbox', { name: 'Message' })).toHaveValue('');
    const p = await selectionPoint(page);
    await page.mouse.click(p.x, p.y);
    await expect(page.getByRole('textbox', { name: 'Message' })).toHaveValue('@U1 ');
    expect(errors).toEqual([]);
    expect(external).toEqual([]);
    expect(parserWarnings).toEqual([]);
  });

  test('board (KiCad 11 transform + nets by name): renders, net clicks do not throw, footprint click → @REF', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await mockApi(page, ECC83);
    await openViewer(page, false);
    await page.getByRole('tab', { name: 'PCB' }).click();
    await ready(page);
    await expect(page.getByRole('alert')).toHaveCount(0);
    const nets = await page.evaluate(() => {
      type BoardViewer = {
        board: { nets: unknown[]; segments: Array<{ netname?: string }>; footprints: Array<{ pads: Array<{ net?: unknown }> }> };
        handleItemClick(item: unknown): void;
      };
      const v = (window as unknown as { __kcViewer: () => BoardViewer }).__kcViewer();
      const seg = v.board.segments[0]!;
      v.handleItemClick(seg); // net highlight: threw on KiCad 11 boards before the patch
      const noNetPad = v.board.footprints.flatMap((f) => f.pads).find((pad) => !pad.net);
      if (noNetPad) v.handleItemClick(noNetPad);
      return { count: v.board.nets.length, segmentNet: seg.netname };
    });
    expect(nets.count).toBeGreaterThan(1);
    expect(nets.segmentNet).toBeTruthy();
    await page.getByRole('textbox', { name: 'Find reference' }).fill('U1');
    await page.getByRole('button', { name: 'Find', exact: true }).click();
    await expect.poll(() => page.evaluate(() => Boolean((window as unknown as { __kcViewer: () => AnyViewer }).__kcViewer().selected))).toBe(true);
    const p = await selectionPoint(page);
    await page.mouse.click(p.x, p.y);
    await expect(page.getByRole('textbox', { name: 'Message' })).toHaveValue(/@U1 /);
    expect(errors).toEqual([]);
  });

  test('hierarchy: sheet tree, breadcrumb back up, Find on another sheet', async ({ page }) => {
    await mockApi(page, HIER);
    await openViewer(page, false);
    await ready(page);
    const crumbs = page.getByRole('navigation', { name: 'Sheet path' });
    await expect(crumbs).toHaveText('Root');
    await page.getByRole('button', { name: 'Sheets (3)' }).click();
    await expect(page.getByRole('treeitem')).toHaveCount(3);
    await page.getByRole('tree').getByRole('button', { name: 'ampli_ht_horizontal' }).click();
    await expect(crumbs).toHaveText(/Root.*ampli_ht_horizontal/);
    await crumbs.getByRole('button', { name: 'Root' }).click();
    await expect(crumbs).toHaveText('Root');
    await page.getByRole('textbox', { name: 'Find reference' }).fill('C301');
    await page.getByRole('button', { name: 'Find', exact: true }).click();
    await expect(crumbs).toHaveText(/Root.*ampli_ht/);
    await expect.poll(() => page.evaluate(() => Boolean((window as unknown as { __kcViewer: () => AnyViewer }).__kcViewer().selected))).toBe(true);
    await page.getByRole('textbox', { name: 'Find reference' }).fill('X999');
    await page.getByRole('button', { name: 'Find', exact: true }).click();
    await expect(page.getByText('X999 not found in the schematic.')).toBeVisible();
  });
});

test.describe('phone', () => {
  test.skip(({ isMobile }) => !isMobile, 'phone only');

  test('pinch zooms about the fingers, double tap zooms in, buttons zoom', async ({ page }) => {
    await mockApi(page, ECC83);
    await openViewer(page, true);
    await ready(page);
    const box = (await page.getByTestId('kicanvas-host').first().boundingBox())!;
    const cx = box.x + box.width / 2;
    const cy = box.y + box.height / 2;
    const cdp = await page.context().newCDPSession(page);
    const touch = (type: 'touchStart' | 'touchMove' | 'touchEnd', pts: Array<[number, number]>) =>
      cdp.send('Input.dispatchTouchEvent', { type, touchPoints: pts.map(([x, y], id) => ({ x, y, id })) });

    const z0 = await zoomOf(page);
    await touch('touchStart', [[cx - 40, cy], [cx + 40, cy]]);
    for (let i = 1; i <= 8; i++) await touch('touchMove', [[cx - 40 - i * 10, cy], [cx + 40 + i * 10, cy]]);
    await touch('touchEnd', []);
    const z1 = await zoomOf(page);
    expect(z1 / z0).toBeGreaterThan(1.8); // distance 80 → 240 px: ~3x, smooth (not fixed steps)

    await page.waitForTimeout(400);
    await page.touchscreen.tap(cx, cy);
    await page.touchscreen.tap(cx, cy);
    await expect.poll(() => zoomOf(page)).toBeGreaterThan(z1 * 1.9);

    const z2 = await zoomOf(page);
    await page.getByRole('button', { name: 'Zoom out' }).first().click();
    expect(await zoomOf(page)).toBeLessThan(z2);
    await page.getByRole('button', { name: 'Fit to view' }).first().click();
    expect(await zoomOf(page)).toBeCloseTo(z0, 1);
  });

  test('tap on a part inserts @REF', async ({ page }) => {
    await mockApi(page, ECC83);
    await openViewer(page, true);
    await ready(page);
    await page.getByRole('textbox', { name: 'Find reference' }).fill('U1');
    await page.getByRole('button', { name: 'Find', exact: true }).click();
    await expect.poll(() => page.evaluate(() => Boolean((window as unknown as { __kcViewer: () => AnyViewer }).__kcViewer().selected))).toBe(true);
    const p = await selectionPoint(page);
    await page.touchscreen.tap(p.x, p.y);
    await page.getByRole('tab', { name: 'Chat' }).click();
    await expect(page.getByRole('textbox', { name: 'Message' })).toHaveValue('@U1 ');
  });

  test('large board: agent view first, interactive view only on request', async ({ page }) => {
    await mockApi(page, { ...ECC83, bytes: { 'ecc83-pp.kicad_pcb': 85_500_000 } });
    await openViewer(page, true);
    await page.getByRole('tab', { name: 'PCB' }).click();
    await expect(page.getByRole('note')).toContainText('85.5 MB');
    await expect(page.locator('kicanvas-embed')).toHaveCount(1); // only the (small) schematic's
    await expect(page.getByRole('img', { name: 'Agent view of the PCB' })).toBeVisible();
    await page.getByRole('button', { name: 'Load interactive view anyway' }).click();
    await expect(page.locator('kicanvas-embed')).toHaveCount(2);
    await ready(page);
  });
});
