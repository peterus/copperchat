import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { KcPage, KcViewer } from '@/lib/kicanvas';
import { useComposer } from '@/stores/composer';
import { KicadPane } from './KicadPane';
import { KICANVAS_SCRIPT, LOAD_GRACE_MS } from './KicanvasPane';
import { insertMention, type KicadPaneProps } from './types';

vi.mock('@/lib/kicanvas', async (orig) => ({
  ...(await orig<typeof import('@/lib/kicanvas')>()),
  loadKicanvas: () => Promise.resolve(),
}));

const PID = 'a1b2c3d4e5f6';

function page(sheet_path: string, filename: string, name: string, refs: string[] = []): KcPage {
  const symbols = new Map(refs.map((r) => [r, { reference: r, instances: new Map([[sheet_path, { reference: r }]]) }]));
  return { type: 'schematic', filename, sheet_path, name, project_path: `${filename}:${sheet_path}`, document: { symbols } };
}
const root = page('/r', 'top.kicad_sch', 'Root', ['J1']);
const power = page('/r/p', 'power.kicad_sch', 'Power');
const reg = page('/r/p/g', 'reg.kicad_sch', 'Regulator', ['U3']);

/** Stand-in for what the patched KiCanvas does inside <kicanvas-embed>. */
class FakeKicanvas {
  inner = document.createElement('div');
  viewer: KcViewer & { selected: unknown } = {
    selected: null,
    select: vi.fn((ref: string | null) => {
      this.viewer.selected = ref && this.current().document?.symbols?.has(ref) ? { ref } : null;
      // KiCanvas dispatches kicanvas:select for every select()
      this.inner.dispatchEvent(
        new CustomEvent('kicanvas:select', { bubbles: true, composed: true, detail: { item: ref ? { reference: ref } : null } }),
      );
    }),
    draw: vi.fn(),
    zoom_to_page: vi.fn(),
    zoom_to_selection: vi.fn(),
    viewport: { zoom_about: vi.fn() },
  };
  project = Object.assign(new EventTarget(), {
    active_page: root as KcPage | null,
    pages: () => [root, power, reg],
    set_active_page: vi.fn((p: KcPage | string) => {
      this.project.active_page = typeof p === 'string' ? ([root, power, reg].find((x) => x.project_path === p) ?? null) : p;
      this.project.dispatchEvent(new Event('change'));
      setTimeout(() => this.load(), 0); // the viewer re-loads the new page
    }),
  });
  constructor(public embed: HTMLElement) {
    Object.assign(this.inner, { viewer: this.viewer });
    embed.appendChild(this.inner);
    Object.assign(embed, { project: this.project });
  }
  current() {
    return this.project.active_page ?? root;
  }
  load() {
    this.inner.dispatchEvent(new CustomEvent('kicanvas:viewerload', { bubbles: true, composed: true }));
  }
  pick(item: unknown) {
    this.inner.dispatchEvent(new CustomEvent('kicanvas:select', { bubbles: true, composed: true, detail: { item } }));
  }
}

const embedEl = () => document.querySelector('kicanvas-embed') as HTMLElement;

async function renderPane(over: Partial<KicadPaneProps> = {}) {
  const props: KicadPaneProps = {
    projectId: PID,
    doc: 'sch',
    files: ['top.kicad_sch', 'sub/reg.kicad_sch'],
    rev: 3,
    theme: 'dark',
    onPick: insertMention,
    ...over,
  };
  const utils = render(<KicadPane {...props} />);
  await act(async () => {});
  return { ...utils, props, rerenderWith: async (more: Partial<KicadPaneProps>) => {
    utils.rerender(<KicadPane {...props} {...more} />);
    await act(async () => {});
  } };
}

async function loadReady() {
  const kc = new FakeKicanvas(embedEl());
  act(() => kc.load());
  await act(async () => {
    vi.advanceTimersByTime(LOAD_GRACE_MS + 10);
  });
  return kc;
}

beforeEach(() => {
  vi.useFakeTimers();
  useComposer.getState().clear();
});
afterEach(() => vi.useRealTimers());

describe('KicanvasPane', () => {
  it('creates an embed with theme, controls and one source per view file', async () => {
    await renderPane();
    const embed = embedEl();
    expect(embed.getAttribute('theme')).toBe('witchhazel');
    expect(embed.getAttribute('controls')).toBe('basic');
    expect(embed.getAttribute('controlslist')).toContain('nooverlay');
    expect(embed.getAttribute('controlslist')).toContain('nobottomtoolbar');
    expect([...embed.querySelectorAll('kicanvas-source')].map((s) => s.getAttribute('src'))).toEqual([
      `/api/projects/${PID}/view/files/top.kicad_sch?v=3`,
      `/api/projects/${PID}/view/files/sub/reg.kicad_sch?v=3`,
    ]);
    expect(screen.getByRole('status')).toHaveTextContent('Loading schematic');
  });

  it('inserts @R12 into the composer on tap, but not for sheets or power symbols', async () => {
    await renderPane();
    const kc = await loadReady();
    act(() => kc.pick({ reference: 'R12' }));
    expect(useComposer.getState().text).toBe('@R12 ');
    act(() => kc.pick({ sheetname: 'Power', sheetfile: 'power.kicad_sch' }));
    act(() => kc.pick({ reference: '#PWR01' }));
    act(() => kc.pick(null));
    expect(useComposer.getState().text).toBe('@R12 ');
  });

  it('fits the drawing once loaded and wires the zoom buttons', async () => {
    await renderPane();
    const kc = await loadReady();
    expect(kc.viewer.zoom_to_page).toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    expect(kc.viewer.viewport?.zoom_about).toHaveBeenCalledWith(1.6);
    fireEvent.click(screen.getByRole('button', { name: 'Zoom out' }));
    expect(kc.viewer.viewport?.zoom_about).toHaveBeenLastCalledWith(1 / 1.6);
    fireEvent.click(screen.getByRole('button', { name: 'Fit to view' }));
    expect(kc.viewer.zoom_to_page).toHaveBeenCalledTimes(2);
  });

  it('treats an error during load as a failure and falls back to the agent view (KiCanvas still fires load)', async () => {
    await renderPane({ doc: 'pcb', files: ['top.kicad_pcb'] });
    const kc = new FakeKicanvas(embedEl());
    act(() => kc.load());
    act(() => {
      window.dispatchEvent(
        new ErrorEvent('error', { message: "Cannot read properties of undefined (reading 'position')", filename: `http://x/${KICANVAS_SCRIPT}` }),
      );
    });
    await act(async () => {
      vi.advanceTimersByTime(LOAD_GRACE_MS + 10);
    });
    expect(screen.getByRole('alert')).toHaveTextContent(/interactive board view failed \(Cannot read properties/);
    expect(screen.getByRole('img', { name: 'Agent view of the PCB' })).toBeInTheDocument();
    expect(embedEl()).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await act(async () => {});
    expect(embedEl()).not.toBeNull();
  });

  it('ignores errors from other app code while loading and after', async () => {
    await renderPane({ doc: 'pcb', files: ['top.kicad_pcb'] });
    const kc = new FakeKicanvas(embedEl());
    act(() => kc.load());
    act(() => {
      window.dispatchEvent(new ErrorEvent('error', { message: 'chat broke', filename: 'http://x/src/stores/chat.ts' }));
      const reason = new Error('fetch failed');
      reason.stack = 'Error: fetch failed\n    at http://x/src/api/http.ts:10:5';
      window.dispatchEvent(Object.assign(new Event('unhandledrejection'), { reason }));
    });
    await act(async () => {
      vi.advanceTimersByTime(LOAD_GRACE_MS + 10);
    });
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByRole('button', { name: 'Fit to view' })).toBeInTheDocument();
    act(() => {
      window.dispatchEvent(new ErrorEvent('error', { message: 'chat broke again' }));
    });
    expect(screen.queryByText(/Viewer error/)).toBeNull();
  });

  it('a double tap on a part inserts a single mention', async () => {
    const now = vi.spyOn(performance, 'now').mockReturnValue(1000);
    await renderPane();
    const kc = await loadReady();
    act(() => kc.pick({ reference: 'U1' }));
    now.mockReturnValue(1250);
    act(() => kc.pick({ reference: 'U1' }));
    expect(useComposer.getState().text).toBe('@U1 ');
    now.mockReturnValue(3000);
    act(() => kc.pick({ reference: 'U1' }));
    expect(useComposer.getState().text).toBe('@U1 @U1 ');
  });

  it('shows errors after the load as a dismissable notice without failing', async () => {
    await renderPane({ doc: 'pcb', files: ['top.kicad_pcb'] });
    await loadReady();
    act(() => {
      window.dispatchEvent(
        new ErrorEvent('error', { message: "Cannot read properties of undefined (reading 'number')", filename: `http://x/${KICANVAS_SCRIPT}` }),
      );
    });
    expect(screen.getByText(/Viewer error: Cannot read properties/)).toBeInTheDocument();
    expect(embedEl()).not.toBeNull();
    await act(async () => {
      vi.advanceTimersByTime(7000);
    });
    expect(screen.queryByText(/Viewer error/)).toBeNull();
  });

  it('shows the sheet hierarchy as a tree with a breadcrumb back up', async () => {
    await renderPane();
    const kc = await loadReady();
    const crumbs = () => screen.getByRole('navigation', { name: 'Sheet path' }).textContent;
    expect(crumbs()).toBe('Root');
    fireEvent.click(screen.getByRole('button', { name: 'Sheets (3)' }));
    const tree = screen.getByRole('tree', { name: 'Sheet hierarchy' });
    expect(tree.querySelectorAll('[role="group"] [role="group"] [role="treeitem"]')).toHaveLength(1); // Regulator at depth 2
    fireEvent.click(screen.getByRole('button', { name: 'Regulator' }));
    expect(kc.project.set_active_page).toHaveBeenCalledWith(reg.project_path);
    expect(crumbs()).toBe('RootPowerRegulator');
    expect(screen.getByRole('treeitem', { name: 'Regulator' })).toHaveAttribute('aria-selected', 'true');
    fireEvent.click(within(screen.getByRole('navigation', { name: 'Sheet path' })).getByRole('button', { name: 'Power' }));
    expect(crumbs()).toBe('RootPower');
  });

  it('finds a reference on another sheet: switches sheet, selects without a mention, zooms', async () => {
    const onFindResult = vi.fn();
    const { rerenderWith } = await renderPane({ onFindResult });
    const kc = await loadReady();
    await rerenderWith({ onFindResult, find: { ref: 'U3', seq: 1 } });
    expect(kc.project.set_active_page).toHaveBeenCalledWith(reg);
    await act(async () => {
      vi.advanceTimersByTime(10);
    });
    expect(kc.viewer.select).toHaveBeenCalledWith('U3');
    expect(kc.viewer.zoom_to_selection).toHaveBeenCalled();
    expect(onFindResult).toHaveBeenCalledWith('U3', true);
    expect(useComposer.getState().text).toBe('');
    await rerenderWith({ onFindResult, find: { ref: 'R999', seq: 2 } });
    expect(onFindResult).toHaveBeenLastCalledWith('R999', false);
  });

  it('reloads on a new rev and restores the sheet that was shown', async () => {
    const { rerenderWith } = await renderPane();
    const kc = await loadReady();
    act(() => kc.project.set_active_page(reg.project_path));
    const first = embedEl();
    await rerenderWith({ rev: 4 });
    expect(embedEl()).not.toBe(first);
    expect(embedEl().querySelector('kicanvas-source')!.getAttribute('src')).toContain('?v=4');
    const kc2 = await loadReady();
    expect(kc2.project.set_active_page).toHaveBeenCalledWith(reg);
  });

  it('changes the theme without a page reload by re-creating the embed', async () => {
    const { rerenderWith } = await renderPane();
    await loadReady();
    await rerenderWith({ theme: 'light' });
    expect(embedEl().getAttribute('theme')).toBe('kicad');
  });
});
