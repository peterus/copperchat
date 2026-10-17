import { Maximize, Minus, Plus } from 'lucide-react';

const btn =
  'flex h-11 w-11 items-center justify-center rounded-full border border-border bg-bg/90 text-fg shadow ' +
  'backdrop-blur hover:bg-surface-2 active:bg-surface-2';

/** Large, thumb-reachable +/−/fit buttons in the bottom-right corner of a pane. */
export function ZoomControls({ onZoomIn, onZoomOut, onFit }: { onZoomIn: () => void; onZoomOut: () => void; onFit: () => void }) {
  return (
    <div
      role="group"
      aria-label="Zoom"
      className="absolute right-3 bottom-3 z-10 flex flex-col gap-2 pb-[env(safe-area-inset-bottom)]"
      // keep taps on the buttons away from the pane's own gesture handling
      onPointerDown={(e) => e.stopPropagation()}
    >
      <button type="button" className={btn} aria-label="Zoom in" onClick={onZoomIn}>
        <Plus size={20} />
      </button>
      <button type="button" className={btn} aria-label="Zoom out" onClick={onZoomOut}>
        <Minus size={20} />
      </button>
      <button type="button" className={btn} aria-label="Fit to view" onClick={onFit}>
        <Maximize size={18} />
      </button>
    </div>
  );
}
