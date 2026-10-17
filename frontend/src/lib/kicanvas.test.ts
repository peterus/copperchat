import { describe, expect, it, vi } from 'vitest';
import {
  breadcrumb,
  buildSheetTree,
  countSheets,
  findRefPage,
  fitViewer,
  type KcPage,
  type KcSymbol,
  type KcViewer,
  kicanvasTheme,
  refOf,
  zoomViewer,
} from './kicanvas';

const sym = (reference: string, instances: Record<string, string> = {}): KcSymbol => ({
  reference,
  instances: new Map(Object.entries(instances).map(([path, r]) => [path, { reference: r }])),
});

function page(sheet_path: string, filename: string, name: string, symbols: KcSymbol[] = []): KcPage {
  return {
    type: 'schematic',
    filename,
    sheet_path,
    name,
    project_path: `${filename}:${sheet_path}`,
    document: { symbols: new Map(symbols.map((s, i) => [String(i), s])) },
  };
}

// Root → Power → Regulator, Root → Amp (placed twice: amp1 and amp2 share amp.kicad_sch)
const root = page('/r', 'top.kicad_sch', 'Root', [sym('J1', { '/r': 'J1' })]);
const power = page('/r/p', 'power.kicad_sch', 'Power');
const reg = page('/r/p/g', 'reg.kicad_sch', 'Regulator', [sym('U3', { '/r/p/g': 'U3' })]);
const amp = sym('U?', { '/r/a1': 'U101', '/r/a2': 'U102' });
const amp1 = page('/r/a1', 'amp.kicad_sch', 'Amp 1', [amp]);
const amp2 = page('/r/a2', 'amp.kicad_sch', 'Amp 2', [amp]);
const board: KcPage = { type: 'pcb', filename: 'top.kicad_pcb', sheet_path: '', name: 'Board', project_path: 'top.kicad_pcb' };
const PAGES = [root, amp1, reg, board, power, amp2];

describe('refOf', () => {
  it('reads the reference of symbols and footprints', () => {
    expect(refOf({ reference: 'R12', lib_id: 'Device:R' })).toBe('R12');
    // KiCad footprints also record the sheet they came from
    expect(refOf({ reference: 'U1', sheetfile: 'amp.kicad_sch', sheetname: 'Amp', pads: [] })).toBe('U1');
  });
  it('ignores sheets, unannotated parts, power symbols and non-objects', () => {
    expect(refOf({ sheetname: 'Power', sheetfile: 'power.kicad_sch', pins: [] })).toBeNull();
    expect(refOf({ reference: 'R?' })).toBeNull();
    expect(refOf({ reference: '#PWR01' })).toBeNull();
    expect(refOf({ reference: '' })).toBeNull();
    expect(refOf(null)).toBeNull();
    expect(refOf(undefined)).toBeNull();
  });
});

describe('sheet hierarchy', () => {
  it('builds a parent/child tree from the sheet paths (pages arrive in any order)', () => {
    const tree = buildSheetTree(PAGES);
    expect(tree.map((n) => n.name)).toEqual(['Root']);
    expect(tree[0]!.children.map((n) => n.name)).toEqual(['Amp 1', 'Power', 'Amp 2']);
    expect(tree[0]!.children[1]!.children.map((n) => n.name)).toEqual(['Regulator']);
    expect(countSheets(tree)).toBe(5);
  });
  it('keeps orphan pages as extra roots and falls back to the file name', () => {
    const orphan = { ...page('/x', 'orphan.kicad_sch', ''), name: undefined };
    const tree = buildSheetTree([root, orphan]);
    expect(tree.map((n) => n.name)).toEqual(['Root', 'orphan.kicad_sch']);
  });
  it('gives the breadcrumb from the root to the active sheet', () => {
    const tree = buildSheetTree(PAGES);
    expect(breadcrumb(tree, reg.project_path).map((n) => n.name)).toEqual(['Root', 'Power', 'Regulator']);
    expect(breadcrumb(tree, root.project_path).map((n) => n.name)).toEqual(['Root']);
    expect(breadcrumb(tree, 'nope')).toEqual([]);
    expect(breadcrumb(tree, null)).toEqual([]);
  });
});

describe('findRefPage', () => {
  it('searches every sheet instance, not only the current sheet', () => {
    expect(findRefPage(PAGES, 'U3')).toBe(reg);
    expect(findRefPage(PAGES, 'j1')).toBe(root);
  });
  it('tells instances of a reused sheet apart by their reference', () => {
    expect(findRefPage(PAGES, 'U101')).toBe(amp1);
    expect(findRefPage(PAGES, 'U102')).toBe(amp2);
  });
  it('returns null when no sheet has the reference', () => {
    expect(findRefPage(PAGES, 'R999')).toBeNull();
  });
});

describe('viewer helpers', () => {
  function fakeViewer(board: boolean): KcViewer {
    return {
      selected: null,
      select: vi.fn(),
      draw: vi.fn(),
      zoom_to_page: vi.fn(),
      zoom_to_selection: vi.fn(),
      ...(board ? { zoom_to_board: vi.fn() } : {}),
      viewport: { zoom_about: vi.fn() },
    };
  }
  it('fits boards to the outline and schematics to the page', () => {
    const b = fakeViewer(true);
    fitViewer(b);
    expect(b.zoom_to_board).toHaveBeenCalled();
    expect(b.draw).toHaveBeenCalled();
    const s = fakeViewer(false);
    fitViewer(s);
    expect(s.zoom_to_page).toHaveBeenCalled();
  });
  it('zooms through the patched viewport', () => {
    const v = fakeViewer(false);
    zoomViewer(v, 1.5);
    expect(v.viewport?.zoom_about).toHaveBeenCalledWith(1.5);
  });
  it('maps the app theme to a KiCanvas theme', () => {
    expect(kicanvasTheme('dark')).toBe('witchhazel');
    expect(kicanvasTheme('light')).toBe('kicad');
  });
});
