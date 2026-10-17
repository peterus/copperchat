import { type ReactNode, useEffect, useState } from 'react';
import { usePanZoom } from '@/hooks/usePanZoom';
import { ZOOM_STEP } from '@/lib/panZoom';
import { agentViewUrl, type DocKind, type PcbLayer } from '@/lib/viewUrls';
import type { ResolvedTheme } from '@/stores/ui';
import { ZoomControls } from './ZoomControls';

const LABEL: Record<DocKind, string> = { sch: 'schematic', pcb: 'PCB' };

export interface AgentViewPaneProps {
  projectId: string;
  doc: DocKind;
  rev: number | null;
  theme: ResolvedTheme;
  layers: readonly PcbLayer[];
  /** Shown above the image (large-board guard, KiCanvas failure). */
  banner?: ReactNode;
}

/** The server-rendered PNG (GET /agent-view) with pan, pinch, double tap and zoom buttons. */
export function AgentViewPane({ banner, ...p }: AgentViewPaneProps) {
  const url = agentViewUrl({ ...p });
  const { ref, style, reset, zoomBy } = usePanZoom();
  const [state, setState] = useState<'loading' | 'loaded' | 'failed'>('loading');
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    setState('loading');
    reset();
  }, [url, reset]);

  return (
    <div className="relative h-full w-full overflow-hidden bg-surface">
      <div ref={ref} className="h-full w-full touch-none select-none" data-testid="agent-view-surface">
        <div style={style} className="h-full w-full">
          <img
            key={`${url}#${attempt}`}
            src={url}
            alt={`Agent view of the ${LABEL[p.doc]}`}
            draggable={false}
            onLoad={() => setState('loaded')}
            onError={() => setState('failed')}
            className="pointer-events-none h-full w-full object-contain"
          />
        </div>
      </div>
      {banner && <div className="absolute inset-x-2 top-2 z-10">{banner}</div>}
      {state === 'loading' && (
        <p role="status" className="pointer-events-none absolute inset-x-0 bottom-3 text-center text-sm text-muted">
          Rendering…
        </p>
      )}
      {state === 'failed' && (
        <div role="alert" className="absolute inset-x-0 top-1/3 flex flex-col items-center gap-2 text-sm text-danger">
          Render failed
          <button
            type="button"
            className="rounded border border-border px-3 py-1 text-fg"
            onClick={() => {
              setState('loading');
              setAttempt((n) => n + 1);
            }}
          >
            Retry
          </button>
        </div>
      )}
      <ZoomControls onZoomIn={() => zoomBy(ZOOM_STEP)} onZoomOut={() => zoomBy(1 / ZOOM_STEP)} onFit={reset} />
    </div>
  );
}
