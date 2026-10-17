import { expect, it, vi } from 'vitest';
import type { ConnectionState } from '@/api/ws';
import { startVisibilityReporter } from './visibility';

const PID = 'a1b2c3d4e5f6';

function fakeWs(open = true) {
  const sent: unknown[] = [];
  let stateCb: (s: ConnectionState) => void = () => {};
  const ws = {
    open,
    send: (f: unknown) => sent.push(f),
    isOpen: () => ws.open,
    onConnection: (cb: (s: ConnectionState) => void) => {
      stateCb = cb;
      return () => {
        stateCb = () => {};
      };
    },
  };
  return { ws, sent, emit: (s: ConnectionState) => stateCb(s) };
}

it('sends visibility for the current project on change and on reconnect', () => {
  const { ws, sent, emit } = fakeWs();
  let hidden = false;
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => (hidden ? 'hidden' : 'visible'));
  const r = startVisibilityReporter(() => PID, ws);
  expect(sent).toEqual([{ type: 'visibility', projectId: PID, visible: true }]);
  hidden = true;
  document.dispatchEvent(new Event('visibilitychange'));
  emit('resumed');
  emit('closed');
  expect(sent.slice(1)).toEqual([
    { type: 'visibility', projectId: PID, visible: false },
    { type: 'visibility', projectId: PID, visible: false },
  ]);
  r.stop();
  document.dispatchEvent(new Event('visibilitychange'));
  expect(sent).toHaveLength(3);
});

it('sends nothing without a project or while the socket is not open', () => {
  const { ws, sent, emit } = fakeWs(false);
  let pid: string | null = PID;
  const r = startVisibilityReporter(() => pid, ws);
  expect(sent).toEqual([]); // never queues a frame (that would open a socket while signed out)
  pid = null;
  ws.open = true;
  emit('open');
  expect(sent).toEqual([]);
  r.stop();
});

it('keeps reporting under the last project after leaving it', () => {
  const { ws, sent } = fakeWs();
  let pid: string | null = PID;
  let hidden = false;
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => (hidden ? 'hidden' : 'visible'));
  const r = startVisibilityReporter(() => pid, ws);
  pid = null; // back on the project picker
  hidden = true;
  r.notify();
  expect(sent).toEqual([
    { type: 'visibility', projectId: PID, visible: true },
    { type: 'visibility', projectId: PID, visible: false },
  ]);
  r.stop();
});
