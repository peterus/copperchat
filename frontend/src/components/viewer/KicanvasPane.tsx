import { useCallback, useEffect, useRef, useState } from 'react';
import { useMediaQuery } from '@/hooks/useMediaQuery';
import {
  buildSheetTree,
  countSheets,
  EMBED_CONTROLS,
  EMBED_CONTROLSLIST,
  findRefPage,
  fitViewer,
  type KcEmbed,
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
import type { FindRequest, KicadPaneProps } from './types';
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

export function KicanvasPane({ projectId, doc, files, rev, theme, onPick, find, onFindResult, visible = true }: KicadPaneProps) {
  const host = useRef<HTMLDivElement>(null);
  const embedRef = useRef<KcEmbed | null>(null);
  const viewerRef = useRef<KcViewer | null>(null);
  const readyRef = useRef(false);
  /** Sheet shown before a reload (theme change, view-updated): restored afterwards. */
  const activeRef = useRef<string | null>(null);
  /** A select() we issue ourselves must not insert a mention. */
  const programmatic = useRef(false);
  const pendingFind = useRef<FindRequest | null>(null);
  const handlers = useRef({ onPick, onFindResult });
  handlers.current = { onPick, onFindResult };

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
      } else if (visibleRef.current) {
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
        const syncActive = () => {
          const id = embed.project?.active_page?.project_path ?? null;
          activeRef.current = id;
          setActive(id);
        };
        // embed-host-integration.patch: fires for every page load (first load, sheet switch).
        embed.addEventListener('kicanvas:viewerload', (e) => {
          const target = e.composedPath()[0] as { viewer?: KcViewer } | undefined;
          viewerRef.current = target?.viewer ?? viewerRef.current;
          if (readyRef.current) {
            // after a sheet switch; KiCanvas clears the selection right after this event
            setTimeout(() => !cancelled && runFind(), 0);
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
              project.addEventListener('change', syncActive);
              offs.push(() => project.removeEventListener('change', syncActive));
              const remembered = pages.find((p) => p.project_path === activeRef.current);
              if (remembered && remembered !== project.active_page) project.set_active_page(remembered);
              syncActive();
            }
            if (viewerRef.current) fitViewer(viewerRef.current);
            setStatus({ kind: 'ready' });
            runFind();
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
  }, [loadKey, runFind]);

  useEffect(() => {
    if (!find) return;
    pendingFind.current = find;
    runFind();
  }, [find, runFind]);

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
