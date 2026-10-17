import type { ClientFrame } from '@/protocol/frames';
import type { ConnectionState } from '@/api/ws';

export interface VisibilityWs {
  send(frame: ClientFrame): void;
  isOpen(): boolean;
  onConnection(cb: (state: ConnectionState) => void): () => void;
}

/**
 * Tells the server whether this page is in the foreground, so push notifications only go out
 * while no CopperChat window is visible. Reported on page visibility changes, on every
 * (re)connect and when the current project changes (`notify()`). Frames are only sent on an
 * open socket: a queued frame would open a connection by itself, even when signed out.
 */
export function startVisibilityReporter(
  getProjectId: () => string | null,
  ws: VisibilityWs,
): { stop: () => void; notify: () => void } {
  // The frame names a project; after leaving a project page the last one is reused, so a
  // page hidden on the project picker still reports itself hidden.
  let last: string | null = null;
  const report = () => {
    const projectId = getProjectId() ?? last;
    if (!projectId || !ws.isOpen()) return;
    last = projectId;
    ws.send({ type: 'visibility', projectId, visible: document.visibilityState === 'visible' });
  };
  report();
  document.addEventListener('visibilitychange', report);
  const offConnection = ws.onConnection((s) => {
    if (s === 'open' || s === 'resumed') report();
  });
  return {
    notify: report,
    stop: () => {
      document.removeEventListener('visibilitychange', report);
      offConnection();
    },
  };
}
