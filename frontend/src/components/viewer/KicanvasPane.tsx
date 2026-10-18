import { useCallback, useEffect, useRef, useState } from 'react';
import { useMediaQuery } from '@/hooks/useMediaQuery';
import {
  buildSheetTree,
  countSheets,
  EMBED_CONTROLS,
  EMBED_CONTROLSLIST,
  findRefPage,
  fitViewer,
  type HighlightKind,
  type KcEmbed,
  type KcPage,
  type KcViewer,
  kicanvasTheme,
  loadKicanvas,
  refOf,
  type SheetNode,
  zoomViewer,
} from '@/lib/kicanvas';
import { DEFAULT_LAYERS, viewFileUrl } from '@/lib/viewUrls';
import { AgentViewPane } from './AgentViewPane';
import { SheetNavigator } from './SheetNavigator';
import type { FindRequest, HighlightRequest, HighlightResult, KicadPaneProps } from './types';
import { ZoomControls } from './ZoomControls';

/**
 * KiCanvas fires its load event even when painting throws (spike 1B), so errors on `window`
 * during the first load — and for this long after it, while the first paint runs — mark the
 * pane failed. Later errors (e.g. a KiCanvas bug on some click) only show a notice.
 */
export const LOAD_GRACE_MS = 400;
const NOTICE_MS = 6000;
const ZOOM_STEP = 1.6;
const DOC_LABEL = { sch: 'schematic', pcb: 'board' } as const;

type Status = { kind: 'loading' } | { kind: 'ready' } | { kind: 'failed'; error: string };

const messageOf = (v: unknown) => (v instanceof Error ? v.message : String(v ?? 'unknown error'));

/** Only errors raised inside the KiCanvas bundle concern the viewer (not chat or other app code). */
export const KICANVAS_SCRIPT = 'vendor/kicanvas/kicanvas.js';
const fromKicanvas = (where: unknown) => typeof where === 'string' && where.includes(KICANVAS_SCRIPT);
/** A double tap clicks twice on the same part; it must insert one mention. */
const SAME_PICK_MS = 500;

/** Network failures of KiCanvas' own fetches may carry no stack frame from the bundle. */
const isNetworkError = (v: unknown) => v instanceof TypeError && /fetch|load failed|network/i.test(v.message);

const refName = (r: HighlightRequest['refs'][number]) => (typeof r === 'string' ? r : r.ref);

/**
 * The page a highlight opens: its `sheetPath`, else the page holding its first ref; null = stay.
 * `undefined` = the requested sheet instance does not exist.
 */
function highlightPage(pages: KcPage[], h: HighlightRequest): KcPage | null | undefined {
  if (h.sheetPath !== null) return pages.find((p) => p.type === 'schematic' && p.sheet_path === h.sheetPath);
  const first = h.refs[0];
  return first === undefined ? null : findRefPage(pages, refName(first));
}

/** The items of `h` that belong on the page `sheetPath`, refs grouped by kind. */
function highlightItems(h: HighlightRequest, sheetPath: string | null) {
  const on = (p: string | null | undefined) => {
    const sp = p === undefined ? h.sheetPath : p;
    return sp === null || sp === sheetPath;
  };
  const refs = new Map<HighlightKind, string[]>();
  for (const r of h.refs) {
    const item = typeof r === 'string' ? { ref: r } : r;
    if (!on(item.sheetPath)) continue;
    const kind = item.kind ?? 'focus';
    const list = refs.get(kind);
    if (list) list.push(item.ref);
    else refs.set(kind, [item.ref]);
  }
  const boxes = h.boxes.filter((b) => on(b.sheetPath)).map(({ x, y, w, h: bh, kind }) => ({ x, y, w, h: bh, kind }));
  return { refs, boxes };
}

export function KicanvasPane({
  projectId,
  doc,
  files,
  rev,
  theme,
  onPick,
  find,
  onFindResult,
  highlight,
  onHighlightResult,
  visible = true,
}: KicadPaneProps) {
  const host = useRef<HTMLDivElement>(null);
  const embedRef = useRef<KcEmbed | null>(null);
  const viewerRef = useRef<KcViewer | null>(null);
  const readyRef = useRef(false);
  /** Sheet shown before a reload (theme change, view-updated): restored afterwards. */
  const activeRef = useRef<string | null>(null);
  /** A select() we issue ourselves must not insert a mention. */
  const programmatic = useRef(false);
  const pendingFind = useRef<FindRequest | null>(null);
  /** The highlight shown; re-applied after every viewer load (KiCanvas resets it in load()). */
  const highlightReq = useRef<HighlightRequest | null>(null);
  /** Per request: its page was opened (`navigated`) and its result reported (`reported`). */
  const highlightProgress = useRef({ seq: -1, navigated: false, reported: false });
  const handlers = useRef({ onPick, onFindResult, onHighlightResult });
  handlers.current = { onPick, onFindResult, onHighlightResult };

  const [status, setStatus] = useState<Status>({ kind: 'loading' });
  const [notice, setNotice] = useState<string | null>(null);
  const [tree, setTree] = useState<SheetNode[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const coarse = useMediaQuery('(pointer: coarse)');

  const urls = files.map((f) => viewFileUrl(projectId, f, rev));
  const srcKey = urls.join('\n');
  const kTheme = kicanvasTheme(theme);
  const visibleRef = useRef(visible);
  visibleRef.current = visible;
  const source = useRef({ urls, kTheme });
  source.current = { urls, kTheme };
  // What should be loaded; applied (loadKey) only while the pane is visible.
  const wantKey = `${srcKey}\n${kTheme}\n${attempt}`;
  const [loadKey, setLoadKey] = useState<string | null>(visible ? wantKey : null);
  /** The embed shown is about to be replaced (new files/theme): do not apply anything to it. */
  const staleRef = useRef(false);
  staleRef.current = loadKey !== wantKey;
  /** A sheet switch is in flight: the viewer still shows the previous page until viewerload. */
  const pageLoading = useRef(false);
  useEffect(() => {
    if (visible) setLoadKey(wantKey);
  }, [visible, wantKey]);

  /** Select `req.ref`, switching to the sheet that contains it first (KiCanvas searches only the current sheet). */
  const runFind = useCallback(() => {
    const req = pendingFind.current;
    const viewer = viewerRef.current;
    const project = embedRef.current?.project;
    if (!req || !viewer || !readyRef.current) return;
    if (doc === 'sch' && project) {
      const page = findRefPage(project.pages(), req.ref);
      if (!page) {
        pendingFind.current = null;
        handlers.current.onFindResult?.(req.ref, false);
        return;
      }
      if (page !== project.active_page) {
        project.set_active_page(page); // the next viewer load runs the find again
        return;
      }
    }
    pendingFind.current = null;
    programmatic.current = true;
    try {
      viewer.select(req.ref);
    } catch {
      // KiCanvas throws for items it cannot box; treated as "not found"
    } finally {
      programmatic.current = false;
    }
    const found = Boolean(viewer.selected);
    if (found) viewer.zoom_to_selection();
    handlers.current.onFindResult?.(req.ref, found);
  }, [doc]);

  /**
   * Applies the highlight to the shown page: opens its page first (like runFind), then outlines
   * the items of the page shown — again after each viewer load, so it follows sheet switches.
   */
  const runHighlight = useCallback(() => {
    const viewer = viewerRef.current;
    const project = embedRef.current?.project;
    if (doc !== 'sch' || !viewer?.clear_highlight || !readyRef.current || staleRef.current || pageLoading.current) return;
    viewer.clear_highlight();
    const h = highlightReq.current;
    if (!h) return;
    const progress = highlightProgress.current;
    const report = (r: HighlightResult) => {
      progress.reported = true;
      handlers.current.onHighlightResult?.(r);
    };
    if (!progress.navigated) {
      progress.navigated = true;
      if (project) {
        const page = highlightPage([...project.pages()], h);
        if (page === undefined) {
          report({ found: [], missing: h.refs.map(refName), sheetMissing: true });
          return;
        }
        if (page && page !== project.active_page) {
          pageLoading.current = true;
          project.set_active_page(page); // the next viewer load applies it
          return;
        }
      }
    }
    const { refs, boxes } = highlightItems(h, project?.active_page?.sheet_path ?? null);
    const found: string[] = [];
    const missing: string[] = [];
    for (const [kind, targets] of refs) {
      const r = viewer.highlight?.(targets, kind) ?? { found: [], missing: targets };
      found.push(...r.found);
      missing.push(...r.missing);
    }
    const drawn = boxes.length ? (viewer.highlight_boxes?.(boxes) ?? 0) : 0;
    if (!progress.reported) {
      if (found.length || drawn) viewer.zoom_to_highlight?.();
      report({ found, missing });
    }
  }, [doc]);

  useEffect(() => {
    const el = host.current;
    if (!el || loadKey === null) return;
    const { urls, kTheme } = source.current;
    let cancelled = false;
    let failed = false;
    let graceTimer: ReturnType<typeof setTimeout> | undefined;
    let noticeTimer: ReturnType<typeof setTimeout> | undefined;
    const offs: Array<() => void> = [];
    readyRef.current = false;
    pageLoading.current = false;
    viewerRef.current = null;
    setStatus({ kind: 'loading' });
    setNotice(null);
    setTree([]);
    setActive(null);

    const onError = (msg: string) => {
      if (cancelled) return;
      if (!readyRef.current) {
        failed = true;
        setStatus({ kind: 'failed', error: msg });
      } else if (pageLoading.current) {
        pageLoading.current = false; // a failed sheet load fires no viewerload
        // a highlight that opened that sheet would otherwise wait silently
        const h = highlightReq.current;
        const progress = highlightProgress.current;
        if (h && progress.navigated && !progress.reported) {
          progress.reported = true;
          handlers.current.onHighlightResult?.(
            { found: [], missing: h.refs.map(refName) },
            `The sheet could not be loaded (${msg}).`,
          );
        }
      }
      if (readyRef.current && visibleRef.current) {
        setNotice(msg);
        clearTimeout(noticeTimer);
        noticeTimer = setTimeout(() => setNotice(null), NOTICE_MS);
      }
    };
    const onWinError = (e: ErrorEvent) => {
      if (fromKicanvas(e.filename) || fromKicanvas((e.error as Error | undefined)?.stack)) {
        onError(e.message || messageOf(e.error));
      }
    };
    const onRejection = (e: PromiseRejectionEvent) => {
      const reason: unknown = e.reason;
      if (fromKicanvas((reason as Error | undefined)?.stack) || (!readyRef.current && isNetworkError(reason))) {
        onError(messageOf(reason));
      }
    };
    window.addEventListener('error', onWinError);
    window.addEventListener('unhandledrejection', onRejection);
    offs.push(() => {
      window.removeEventListener('error', onWinError);
      window.removeEventListener('unhandledrejection', onRejection);
    });

    void loadKicanvas().then(
      () => {
        if (cancelled) return;
        const embed = document.createElement('kicanvas-embed') as KcEmbed;
        embed.setAttribute('controls', EMBED_CONTROLS);
        embed.setAttribute('controlslist', EMBED_CONTROLSLIST);
        embed.setAttribute('theme', kTheme);
        embed.style.display = 'block';
        embed.style.width = '100%';
        embed.style.height = '100%';
        for (const url of urls) {
          const source = document.createElement('kicanvas-source');
          source.setAttribute('src', url);
          embed.appendChild(source);
        }
        // embed-select-event.patch: selection bubbles out of the shadow DOM.
        let lastPick: { ref: string; at: number } | null = null;
        embed.addEventListener('kicanvas:select', (e) => {
          if (programmatic.current) return;
          const ref = refOf((e as CustomEvent<{ item?: unknown }>).detail?.item);
          if (!ref) return;
          const at = performance.now();
          if (lastPick?.ref === ref && at - lastPick.at < SAME_PICK_MS) return;
          lastPick = { ref, at };
          handlers.current.onPick(ref);
        });
        const onPageChange = () => {
          pageLoading.current = true; // until the new page's viewerload
          syncActive();
        };
        const syncActive = () => {
          const id = embed.project?.active_page?.project_path ?? null;
          activeRef.current = id;
          setActive(id);
        };
        // embed-host-integration.patch: fires for every page load (first load, sheet switch).
        embed.addEventListener('kicanvas:viewerload', (e) => {
          const target = e.composedPath()[0] as { viewer?: KcViewer } | undefined;
          viewerRef.current = target?.viewer ?? viewerRef.current;
          pageLoading.current = false;
          if (readyRef.current) {
            // after a sheet switch; KiCanvas clears the selection right after this event
            setTimeout(() => {
              if (cancelled) return;
              runFind();
              runHighlight();
            }, 0);
            return;
          }
          clearTimeout(graceTimer);
          graceTimer = setTimeout(() => {
            if (cancelled || failed) return;
            readyRef.current = true;
            const project = embed.project;
            if (project) {
              const pages = [...project.pages()];
              setTree(buildSheetTree(pages));
              project.addEventListener('change', onPageChange);
              offs.push(() => project.removeEventListener('change', onPageChange));
              const remembered = pages.find((p) => p.project_path === activeRef.current);
              if (remembered && remembered !== project.active_page) project.set_active_page(remembered);
              syncActive();
            }
            if (viewerRef.current) fitViewer(viewerRef.current);
            setStatus({ kind: 'ready' });
            // after restoring the remembered sheet, its viewerload runs them on the right page
            if (pageLoading.current) return;
            runFind();
            runHighlight();
          }, LOAD_GRACE_MS);
        });
        embedRef.current = embed;
        el.replaceChildren(embed);
      },
      (e: unknown) => onError(messageOf(e)),
    );

    return () => {
      cancelled = true;
      clearTimeout(graceTimer);
      clearTimeout(noticeTimer);
      offs.forEach((off) => off());
      embedRef.current = null;
      viewerRef.current = null;
      readyRef.current = false;
      el.replaceChildren();
    };
  }, [loadKey, runFind, runHighlight]);

  useEffect(() => {
    if (!find) return;
    pendingFind.current = find;
    runFind();
  }, [find, runFind]);

  useEffect(() => {
    const h = highlight ?? null;
    highlightReq.current = h;
    if (h && h.seq !== highlightProgress.current.seq) highlightProgress.current = { seq: h.seq, navigated: false, reported: false };
    runHighlight();
  }, [highlight, runHighlight]);

  const openSheet = (id: string) => embedRef.current?.project?.set_active_page(id);
  const withViewer = (fn: (v: KcViewer) => void) => () => viewerRef.current && fn(viewerRef.current);

  if (status.kind === 'failed') {
    return (
      <AgentViewPane
        projectId={projectId}
        doc={doc}
        rev={rev}
        theme={theme}
        layers={DEFAULT_LAYERS}
        banner={
          <div role="alert" className="flex flex-wrap items-center gap-2 rounded border border-danger bg-bg/95 px-3 py-2 text-sm shadow">
            <span className="min-w-0 flex-1">
              The interactive {DOC_LABEL[doc]} view failed ({status.error}). Showing the agent view instead.
            </span>
            <button
              type="button"
              className="rounded border border-border px-2 py-1"
              onClick={() => {
                setStatus({ kind: 'loading' });
                setAttempt((n) => n + 1);
              }}
            >
              Retry
            </button>
          </div>
        }
      />
    );
  }

  return (
    <div className="relative h-full w-full overflow-hidden bg-surface" style={{ touchAction: 'none' }}>
      <div ref={host} data-testid="kicanvas-host" className="h-full w-full" />
      {status.kind === 'loading' && (
        <p role="status" className="pointer-events-none absolute inset-x-0 top-1/3 text-center text-sm text-muted">
          Loading {DOC_LABEL[doc]}…
        </p>
      )}
      {status.kind === 'ready' && countSheets(tree) > 1 && (
        <SheetNavigator tree={tree} active={active} onOpen={openSheet} closeOnPick={coarse} />
      )}
      {notice && (
        <div role="status" className="absolute inset-x-2 bottom-3 z-10 mr-14 flex items-start gap-2 rounded border border-warning bg-bg/95 px-3 py-2 text-xs shadow">
          <span className="min-w-0 flex-1">Viewer error: {notice}</span>
          <button type="button" aria-label="Dismiss" className="text-muted" onClick={() => setNotice(null)}>
            ×
          </button>
        </div>
      )}
      {status.kind === 'ready' && (
        <ZoomControls
          onZoomIn={withViewer((v) => zoomViewer(v, ZOOM_STEP))}
          onZoomOut={withViewer((v) => zoomViewer(v, 1 / ZOOM_STEP))}
          onFit={withViewer(fitViewer)}
        />
      )}
    </div>
  );
}
