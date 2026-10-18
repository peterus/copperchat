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
  /** Directory holding `dir` (default: the backend fixture projects). */
  root?: string;
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
  const file = (name: string) => readFileSync(`${fx.root ?? FIXTURES}${fx.dir}/${name}`);
  const bytes = Object.fromEntries([...fx.sch, ...fx.pcb].map((n) => [n, fx.bytes?.[n] ?? file(n).length]));
  await page.route('**/api/auth/me', (r) => r.fulfill({ json: { username: 'admin' } }));
  await page.route('**/api/projects', (r) =>
    r.fulfill({ json: [{ id: PID, name: fx.dir, relPath: `${fx.dir}/x.kicad_pro`, hasSch: true, hasPcb: fx.pcb.length > 0, locked: false }] }),
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

// ---- highlight overlay (fork commit "schematic highlight overlay", contract E-C13) -------------

type HlKind = 'added' | 'modified' | 'removed' | 'focus';
type HlViewer = AnyViewer & {
  highlight(targets: string[], kind?: HlKind): { found: string[]; missing: string[] };
  highlight_boxes(boxes: Array<{ x: number; y: number; w: number; h: number; kind?: HlKind }>): number;
  clear_highlight(): void;
  zoom_to_highlight(): void;
  highlighted_count: number;
  highlight_color(kind: HlKind): { r: number; g: number; b: number };
  theme: { background: { r: number; g: number; b: number } };
  select(item: string | null): void;
  schematic: { find_symbol(ref: string): unknown };
  layers: { query_item_bboxes(item: unknown): Iterable<{ x: number; y: number; w: number; h: number }> };
};

/** Runs `fn` against the visible schematic viewer in the page. */
function onViewer<A, R>(page: Page, fn: (v: HlViewer, arg: A) => R, arg: A): Promise<R> {
  return page.evaluate(
    ([src, a]) => {
      const v = (window as unknown as { __kcViewer: () => HlViewer }).__kcViewer();
      return (new Function('v', 'a', `return (${src})(v, a);`) as (v: HlViewer, a: unknown) => R)(v, a);
    },
    [fn.toString(), arg as unknown] as const,
  ) as Promise<R>;
}

/** Canvas pixels within ±40 per channel of `kind`'s highlight colour (the canvas is Canvas2D). */
async function colorPixels(page: Page, kind: HlKind): Promise<number> {
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  return onViewer(
    page,
    (v, k) => {
      const c = v.highlight_color(k);
      const want = [c.r * 255, c.g * 255, c.b * 255];
      const ctx = v.canvas.getContext('2d')!;
      const d = ctx.getImageData(0, 0, v.canvas.width, v.canvas.height).data;
      let n = 0;
      for (let i = 0; i < d.length; i += 4) {
        if (Math.abs(d[i]! - want[0]!) < 40 && Math.abs(d[i + 1]! - want[1]!) < 40 && Math.abs(d[i + 2]! - want[2]!) < 40) n++;
      }
      return n;
    },
    kind,
  );
}

const canvasHash = (page: Page) =>
  page.evaluate(async () => {
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    const v = (window as unknown as { __kcViewer: () => HlViewer }).__kcViewer();
    const url = v.canvas.toDataURL('image/png');
    const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(url));
    return Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, '0')).join('');
  });

/** WCAG contrast ratio of two 0..1 colours. */
function contrast(a: { r: number; g: number; b: number }, b: { r: number; g: number; b: number }): number {
  const lum = (c: { r: number; g: number; b: number }) => {
    const ch = (x: number) => (x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4);
    return 0.2126 * ch(c.r) + 0.7152 * ch(c.g) + 0.0722 * ch(c.b);
  };
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

/** C105 in the KiCad-generated `kicad-cli sch diff` of spike 2C (`hier_edit`): bbox in 100 nm. */
const C105_DIFF_BBOX = [1664990, 656589, 168453, 83822] as const;

test.describe('highlight overlay', () => {
  test.skip(({ isMobile }) => isMobile, 'desktop only');

  test('highlight set of refs: found, missing, pixels, clear restores the identical canvas', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await mockApi(page, ECC83);
    await openViewer(page, false);
    await ready(page);
    const before = await canvasHash(page);
    expect(await colorPixels(page, 'modified')).toBe(0);
    const res = await onViewer(page, (v) => v.highlight(['U1', 'R3', 'R1', 'R2', 'C1', 'NOPE99'], 'modified'), null);
    expect(res.found).toEqual(['U1', 'R3', 'R1', 'R2', 'C1']);
    expect(res.missing).toEqual(['NOPE99']);
    expect(await onViewer(page, (v) => v.highlighted_count, null)).toBe(5);
    expect(await colorPixels(page, 'modified')).toBeGreaterThan(200);
    await onViewer(page, (v) => v.clear_highlight(), null);
    expect(await onViewer(page, (v) => v.highlighted_count, null)).toBe(0);
    expect(await canvasHash(page)).toBe(before);
    expect(errors).toEqual([]);
  });

  test('highlight survives a selection and the selection a highlight', async ({ page }) => {
    await mockApi(page, ECC83);
    await openViewer(page, false);
    await ready(page);
    await onViewer(page, (v) => v.highlight(['U1', 'R1'], 'added'), null);
    await onViewer(page, (v) => v.select('R3'), null);
    expect(await onViewer(page, (v) => v.highlighted_count, null)).toBe(2);
    expect(await colorPixels(page, 'added')).toBeGreaterThan(100);
    await onViewer(page, (v) => v.highlight(['C1'], 'removed'), null);
    expect(await onViewer(page, (v) => Boolean(v.selected), null)).toBe(true);
    expect(await onViewer(page, (v) => v.highlighted_count, null)).toBe(3);
  });

  test('zoom_to_highlight fits the camera to the highlights', async ({ page }) => {
    await mockApi(page, ECC83);
    await openViewer(page, false);
    await ready(page);
    const z0 = await zoomOf(page);
    await onViewer(page, (v) => v.zoom_to_highlight(), null); // nothing highlighted: no-op
    expect(await zoomOf(page)).toBe(z0);
    await onViewer(page, (v) => v.highlight(['R1'], 'focus'), null);
    await onViewer(page, (v) => v.zoom_to_highlight(), null);
    expect(await zoomOf(page)).toBeGreaterThan(z0 * 2);
    expect(await colorPixels(page, 'focus')).toBeGreaterThan(100);
  });

  test('reset on page switch; refs of another sheet instance are missing until it is loaded', async ({ page }) => {
    await mockApi(page, HIER);
    await openViewer(page, false);
    await ready(page);
    const crumbs = page.getByRole('navigation', { name: 'Sheet path' });
    const res = await onViewer(page, (v) => v.highlight(['C105', 'R201', 'R301'], 'modified'), null);
    expect(res).toEqual({ found: ['C105'], missing: ['R201', 'R301'] });
    await page.getByRole('button', { name: 'Sheets (3)' }).click();
    await page.getByRole('tree').getByRole('button', { name: 'ampli_ht_vertical' }).click();
    await expect(crumbs).toHaveText(/Root.*ampli_ht_vertical/);
    await expect.poll(() => onViewer(page, (v) => v.highlighted_count, null)).toBe(0);
    expect(await onViewer(page, (v) => v.highlight(['R201', 'R301'], 'modified'), null)).toEqual({ found: ['R201'], missing: ['R301'] });
  });

  test('boxes in mm: the diff box of C105 lies within 0.5 mm of the symbol box', async ({ page }) => {
    await mockApi(page, HIER);
    await openViewer(page, false);
    await ready(page);
    const [x, y, w, h] = C105_DIFF_BBOX.map((v) => v / 1e4) as [number, number, number, number];
    expect(await onViewer(page, (v, b) => v.highlight_boxes([{ ...b, kind: 'modified' }]), { x, y, w, h })).toBe(1);
    expect(await onViewer(page, (v) => v.highlight_boxes([{ x: Number.NaN, y: 0, w: 1, h: 1 }]), null)).toBe(0);
    expect(await colorPixels(page, 'modified')).toBeGreaterThan(50);
    const sym = await onViewer(page, (v) => {
      const bb = [...v.layers.query_item_bboxes(v.schematic.find_symbol('C105'))][0]!;
      return { x: bb.x, y: bb.y, w: bb.w, h: bb.h };
    }, null);
    expect(Math.abs(sym.x - x)).toBeLessThan(0.5);
    expect(Math.abs(sym.y - y)).toBeLessThan(0.5);
    expect(Math.abs(sym.y + sym.h - (y + h))).toBeLessThan(0.5);
    // the diff ran on the edited copy, where C105's Value text grew: only the width differs
    expect(w).toBeGreaterThan(sym.w - 0.5);
  });

  for (const scheme of ['light', 'dark'] as const) {
    test.describe(`${scheme} theme`, () => {
      test.use({ colorScheme: scheme });

      test('colours per kind: green / orange / red / focus, distinct and readable', async ({ page }) => {
        await mockApi(page, ECC83);
        await openViewer(page, false);
        await ready(page);
        expect(await page.locator('kicanvas-embed').first().getAttribute('theme')).toBe(scheme === 'dark' ? 'witchhazel' : 'kicad');
        const kinds: HlKind[] = ['added', 'modified', 'removed', 'focus'];
        const colors = await onViewer(page, (v, ks) => ks.map((k) => v.highlight_color(k)), kinds);
        const bg = await onViewer(page, (v) => v.theme.background, null);
        const [added, modified, removed] = colors as [typeof bg, typeof bg, typeof bg];
        expect(added.g).toBeGreaterThan(Math.max(added.r, added.b)); // green
        expect(removed.r).toBeGreaterThan(Math.max(removed.g, removed.b)); // red
        expect(modified.r).toBeGreaterThan(modified.g);
        expect(modified.g).toBeGreaterThan(modified.b); // orange
        expect(new Set(colors.map((c) => `${c.r},${c.g},${c.b}`)).size).toBe(4);
        for (const c of colors) expect(contrast(c, bg)).toBeGreaterThan(2.5);
        const base: number[] = [];
        for (const k of ['added', 'modified', 'removed'] as const) base.push(await colorPixels(page, k));
        await onViewer(page, (v) => {
          v.highlight(['U1'], 'added');
          v.highlight(['R3'], 'modified');
          v.highlight(['R1'], 'removed');
        }, null);
        // R1/R3 are small at page zoom: their outlines alone are ~20 px
        for (const [i, k] of (['added', 'modified', 'removed'] as const).entries()) {
          expect(await colorPixels(page, k)).toBeGreaterThan(base[i]! + 10);
        }
      });
    });
  }
});

// ---- Changes panel → viewer highlight (E9a), real bundle on complex_hierarchy ------------------

const ROOT_SHEET = '/5b9623a5-6d01-41fc-9865-e1bc779418c8';
const VERT_SHEET = `${ROOT_SHEET}/00000000-0000-0000-0000-00004b3a1333`;
const SNAP = 'c'.repeat(32);

/** Spike 2C `hier_edit` as the change log and compacted diff would report it (diff boxes from kicad-cli). */
async function mockChanges(page: Page) {
  const entry = (id: number, op: string, kind: string, uuid: string, sheetPath: string, ref: string | null) => ({
    id, runId: 'run-1', planStepId: null, callSeq: id, tool: op === 'add' ? 'sch_add_wire' : 'sch_set_fields', op, kind, uuid,
    sheetPath, ref, net: null, before: null, after: null, sideEffect: false, createdAt: '2026-10-18T11:00:00Z',
  });
  const box = (b: number[]) => ({ x: b[0]! / 1e4, y: b[1]! / 1e4, w: b[2]! / 1e4, h: b[3]! / 1e4 });
  await page.route(`**/api/projects/${PID}/changes/runs**`, (r) =>
    r.fulfill({ json: [{ runId: 'run-1', stepIds: [], count: 3, sideEffects: 0, erc: null, snapshotId: SNAP, createdAt: '2026-10-18T11:00:00Z' }] }),
  );
  await page.route(`**/api/projects/${PID}/changes?runId=run-1`, (r) =>
    r.fulfill({
      json: [
        entry(1, 'modify', 'symbol', '00000000-0000-0000-0000-00004b4b15e7', ROOT_SHEET, 'C105'),
        entry(2, 'modify', 'symbol', '00000000-0000-0000-0000-00004b4f363e', VERT_SHEET, 'R201'),
        entry(3, 'add', 'wire', '77b3cc87-3ffb-4f0b-89eb-c28e0fc9f8ce', VERT_SHEET, null),
      ],
    }),
  );
  const diffEntry = (sheet: string, uuid: string, kind: string, type: string, ref: string | null, b: number[]) => ({
    id: `${sheet}/${uuid}`, kind, type, uuid, ref, boxMm: box(b), properties: [], sideEffect: false,
  });
  await page.route(`**/api/projects/${PID}/changes/diff**`, (r) =>
    r.fulfill({
      json: {
        snapshotId: SNAP,
        count: 3,
        sheets: {
          [ROOT_SHEET]: [diffEntry(ROOT_SHEET, '00000000-0000-0000-0000-00004b4b15e7', 'modified', 'SCH_SYMBOL', 'C105', [1664990, 656589, 168453, 83822])],
          [VERT_SHEET]: [
            diffEntry(VERT_SHEET, '00000000-0000-0000-0000-00004b4f363e', 'modified', 'SCH_SYMBOL', 'R201', [556159, 467856, 132283, 51574]),
            diffEntry(VERT_SHEET, '77b3cc87-3ffb-4f0b-89eb-c28e0fc9f8ce', 'added', 'SCH_LINE', null, [999238, 999238, 101525, 1525]),
          ],
        },
      },
    }),
  );
  await page.route(`**/api/projects/${PID}/history/${SNAP}/preview`, (r) => r.fulfill({ body: PNG, contentType: 'image/png' }));
}

const highlighted = (page: Page) => onViewer(page, (v) => v.highlighted_count, null);

test.describe('changes panel', () => {
  test('desktop: an entry on a sub-sheet opens that sheet and highlights it; diff boxes; clear', async ({ page, isMobile }) => {
    test.skip(isMobile, 'desktop only');
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await mockApi(page, HIER);
    await mockChanges(page);
    await openViewer(page, false);
    await ready(page);
    await page.getByRole('button', { name: 'Changes' }).click();
    const panel = page.getByRole('dialog', { name: 'Changes' });
    await panel.getByRole('button', { name: 'Show changes' }).click();
    await panel.getByRole('button', { name: /Modified symbol R201/ }).click();
    const crumbs = page.getByRole('navigation', { name: 'Sheet path' });
    await expect(crumbs).toHaveText(/Root.*ampli_ht_vertical/);
    await expect.poll(() => highlighted(page)).toBe(1);
    await expect(page.getByRole('button', { name: 'Clear highlight' })).toBeVisible();
    // the diff layer: both boxes of this sheet; on the root page its own box
    await panel.getByRole('button', { name: 'Diff' }).click();
    await expect.poll(() => highlighted(page)).toBe(1); // opens the first sheet with changes: root (C105)
    await expect(crumbs).toHaveText('Root');
    await page.getByRole('button', { name: 'Sheets (3)' }).click();
    await page.getByRole('tree').getByRole('button', { name: 'ampli_ht_vertical' }).click();
    await expect.poll(() => highlighted(page)).toBe(2);
    await page.getByRole('button', { name: 'Clear highlight' }).click();
    await expect.poll(() => highlighted(page)).toBe(0);
    expect(errors).toEqual([]);
  });

  test('phone: tapping an entry in More › Changes switches to the View tab with the highlight', async ({ page, isMobile }) => {
    test.skip(!isMobile, 'phone only');
    await mockApi(page, HIER);
    await mockChanges(page);
    await openViewer(page, true);
    await ready(page);
    await page.getByRole('tab', { name: 'More' }).click();
    await page.getByRole('button', { name: 'Changes' }).click();
    await page.getByRole('button', { name: 'Show changes' }).click();
    await page.getByRole('button', { name: /Modified symbol C105/ }).click();
    await expect(page.getByRole('tab', { name: 'View' })).toHaveAttribute('aria-selected', 'true');
    await expect.poll(() => highlighted(page)).toBe(1);
    await expect(page.getByRole('button', { name: 'Clear highlight' })).toBeVisible();
  });
});

/** Schematics edited through the agent tools by the golden scenario tests and saved by KiCad
 * (scripts/make-edited-fixtures.py): they load in KiCanvas without errors (spec §12 round trip). */
const EDITED = fileURLToPath(new URL('./fixtures/edited/', import.meta.url));

test.describe('edited fixtures', () => {
  test.skip(({ isMobile }) => isMobile, 'desktop only');

  for (const scenario of ['s1_led', 's2_ldo', 's3_mcu_i2c_subsheet']) {
    test(`${scenario} renders without errors`, async ({ page }) => {
      const manifest = JSON.parse(readFileSync(`${EDITED}${scenario}/manifest.json`, 'utf8')) as { sch: string[]; refs: string[] };
      const errors: string[] = [];
      page.on('pageerror', (e) => errors.push(e.message));
      page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
      await mockApi(page, { dir: scenario, root: EDITED, sch: manifest.sch, pcb: [] });
      await openViewer(page, false);
      await ready(page);
      const res = await onViewer(page, (v, refs) => v.highlight(refs, 'focus'), manifest.refs);
      expect(res.found.length).toBeGreaterThanOrEqual(1);
      expect(manifest.refs).toEqual(expect.arrayContaining(res.found));
      expect(errors).toEqual([]);
    });
  }
});
