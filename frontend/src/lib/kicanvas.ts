/**
 * Glue between React and the vendored KiCanvas bundle (public/vendor/kicanvas, built from the
 * peterus/kicanvas fork, see vendor-patches/kicanvas/README.md). Only the small surface below
 * is used; the shapes are the KiCanvas internals at the pinned commit (spike 1B,
 * docs/spikes/1b-kicanvas.md).
 */
import type { ResolvedTheme } from '@/stores/ui';

const SCRIPT_SRC = '/vendor/kicanvas/kicanvas.js';
let loading: Promise<void> | null = null;

/** Loads the bundle once; resolves when <kicanvas-embed> is defined. */
export function loadKicanvas(): Promise<void> {
  if (!loading) {
    loading = new Promise<void>((resolve, reject) => {
      const s = document.createElement('script');
      s.type = 'module';
      s.src = SCRIPT_SRC;
      s.onerror = () => {
        loading = null;
        s.remove();
        reject(new Error('The KiCanvas viewer could not be loaded.'));
      };
      document.head.appendChild(s);
      void customElements.whenDefined('kicanvas-embed').then(() => resolve());
    });
  }
  return loading;
}

/**
 * KiCanvas ships two themes. Schematics follow the app theme; boards are dark in both (KiCanvas
 * has no light board theme). Passed as the embed's `theme` attribute (embed-host-integration.patch).
 */
export function kicanvasTheme(t: ResolvedTheme): string {
  return t === 'dark' ? 'witchhazel' : 'kicad';
}

/** Embed attributes: interactive, no focus overlay (it eats the first tap), our own toolbar. */
export const EMBED_CONTROLS = 'basic';
export const EMBED_CONTROLSLIST = 'nooverlay nodownload nofullscreen noflipview nobottomtoolbar';

// ---- KiCanvas object shapes (structural, only what we touch) ---------------------------------

export interface KcSymbol {
  reference?: string;
  instances?: Map<string, { reference?: string }>;
}
export interface KcPage {
  type: 'pcb' | 'schematic';
  filename: string;
  sheet_path: string;
  name?: string;
  page?: string;
  project_path: string;
  document?: { symbols?: Map<string, KcSymbol> } | null;
}
export interface KcProject extends EventTarget {
  pages(): Iterable<KcPage>;
  active_page: KcPage | null;
  set_active_page(page: KcPage | string): void;
}
export interface KcViewer {
  selected: unknown;
  select(item: string | null): void;
  draw(): void;
  zoom_to_page(): void;
  zoom_to_selection(): void;
  zoom_to_board?: () => void;
  viewport?: { zoom_about?: (factor: number) => void };
}
/** The <kicanvas-embed> element with the embed-project-getter patch. */
export interface KcEmbed extends HTMLElement {
  project?: KcProject;
}

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null;

/**
 * The reference of a picked item (schematic symbol or footprint), or null for things that cannot
 * be @mentioned: hierarchical sheets (no `reference`), unannotated parts (`R?`) and power
 * symbols (`#PWR01`). Class names are not used: the bundle may be minified. (Footprints also
 * carry a `sheetfile`, so that is no sheet marker.)
 */
export function refOf(item: unknown): string | null {
  if (!isObj(item)) return null;
  const ref = item.reference;
  if (typeof ref !== 'string' || !ref || ref.endsWith('?') || ref.startsWith('#')) return null;
  return ref;
}

// ---- sheet hierarchy ---------------------------------------------------------------------------

export interface SheetNode {
  /** KiCanvas `project_path` (`file:sheet_path`), the key for `set_active_page`. */
  id: string;
  name: string;
  filename: string;
  sheetPath: string;
  children: SheetNode[];
}

const parentPath = (p: string) => p.slice(0, Math.max(0, p.lastIndexOf('/')));

/**
 * The schematic pages as a tree: a sheet instance `/root/a/b` is the child of `/root/a`.
 * The root page is named "Root" by KiCanvas. Pages without a known parent (orphans) are roots.
 */
export function buildSheetTree(pages: Iterable<KcPage>): SheetNode[] {
  const nodes = [...pages]
    .filter((p) => p.type === 'schematic')
    .map((p): SheetNode => ({
      id: p.project_path,
      name: p.name || p.filename,
      filename: p.filename,
      sheetPath: p.sheet_path,
      children: [],
    }));
  const byPath = new Map(nodes.filter((n) => n.sheetPath).map((n) => [n.sheetPath, n]));
  const roots: SheetNode[] = [];
  for (const n of nodes) {
    const parent = n.sheetPath ? byPath.get(parentPath(n.sheetPath)) : undefined;
    if (parent && parent !== n) parent.children.push(n);
    else roots.push(n);
  }
  return roots;
}

/** Root-to-node path of the node `id` (empty when not in the tree). */
export function breadcrumb(tree: SheetNode[], id: string | null): SheetNode[] {
  if (!id) return [];
  for (const n of tree) {
    if (n.id === id) return [n];
    const below = breadcrumb(n.children, id);
    if (below.length) return [n, ...below];
  }
  return [];
}

export function countSheets(tree: SheetNode[]): number {
  return tree.reduce((sum, n) => sum + 1 + countSheets(n.children), 0);
}

/**
 * The schematic page that contains `ref`. KiCanvas' own `select(ref)` only searches the current
 * sheet; a symbol's reference per sheet instance lives in `symbol.instances[sheet_path]` (one
 * sheet file can be placed several times with different references).
 */
export function findRefPage(pages: Iterable<KcPage>, ref: string): KcPage | null {
  const want = ref.toUpperCase();
  for (const page of pages) {
    if (page.type !== 'schematic') continue;
    for (const sym of page.document?.symbols?.values() ?? []) {
      const r = sym.instances?.get(page.sheet_path)?.reference ?? sym.reference;
      if (r?.toUpperCase() === want) return page;
    }
  }
  return null;
}

/** +/− buttons: zoom about the viewport centre (touch-pan-zoom.patch adds `zoom_about`). */
export function zoomViewer(viewer: KcViewer, factor: number): void {
  viewer.viewport?.zoom_about?.(factor);
}

/** Fit: the board outline for boards (the embed's zoom attribute is not forwarded), else the page. */
export function fitViewer(viewer: KcViewer): void {
  if (viewer.zoom_to_board) viewer.zoom_to_board();
  else viewer.zoom_to_page();
  viewer.draw();
}
