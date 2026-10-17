import { useState } from 'react';
import { useMediaQuery } from '@/hooks/useMediaQuery';
import type { PcbLayer } from '@/lib/viewUrls';
import { formatBytes, guardFor, useViewerPrefs } from '@/stores/viewerPrefs';
import { AgentViewPane } from './AgentViewPane';
import { KicadPane } from './KicadPane';
import type { KicadPaneProps } from './types';

const DOC_LABEL = { sch: 'schematic', pcb: 'board' } as const;

/**
 * The KiCad view of one document, behind the large-board guard: above the per-device size limit
 * the agent-view PNG is shown with an explicit "load interactive view anyway" (an 85 MB board
 * crashed an iPhone Safari tab).
 */
export function KicadDocView(props: KicadPaneProps & { bytes: number; layers: readonly PcbLayer[] }) {
  const { bytes, layers, ...pane } = props;
  const mobile = useMediaQuery('(pointer: coarse)');
  const limits = useViewerPrefs((s) => s.limits);
  const [override, setOverride] = useState(false);

  if (pane.files.length === 0) {
    return (
      <div className="flex h-full items-center justify-center bg-surface p-4 text-center text-sm text-muted">
        This project has no {DOC_LABEL[pane.doc]}.
      </div>
    );
  }
  const guard = guardFor(bytes, mobile, limits);
  if (guard && !override) {
    return (
      <AgentViewPane
        projectId={pane.projectId}
        doc={pane.doc}
        rev={pane.rev}
        theme={pane.theme}
        layers={layers}
        banner={
          <div role="note" className="flex flex-wrap items-center gap-2 rounded border border-warning bg-bg/95 px-3 py-2 text-sm shadow">
            <span className="min-w-0 flex-1">
              This {DOC_LABEL[pane.doc]} is large ({formatBytes(guard.bytes)}, limit {guard.limitMb} MB on this device).{' '}
              {guard.mobile
                ? 'The interactive view may crash this browser tab, so the agent view is shown.'
                : 'The interactive view may take minutes to load and use a lot of memory, so the agent view is shown.'}
            </span>
            <button
              type="button"
              className="rounded border border-border px-2 py-1 font-medium"
              onClick={() => setOverride(true)}
            >
              Load interactive view anyway
            </button>
          </div>
        }
      />
    );
  }
  return <KicadPane {...pane} />;
}
