import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { FakeWebSocket } from '@/test/fakeWebSocket';
import { backoffDelay, createWsClient, type ConnectionState } from './ws';

// Project ids are 12 lowercase hex chars on the wire (backend ProjectId).
const P1 = 'a1b2c3d4e5f6';
const runStarted = { type: 'run-started', projectId: P1, runId: 'r1' };

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true });
  document.dispatchEvent(new Event('visibilitychange'));
}

describe('ws client', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeWebSocket.instances = [];
    vi.stubGlobal('WebSocket', FakeWebSocket);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('backoff is exponential from 0.5 s and capped at 15 s', () => {
    expect([0, 1, 2, 3, 4, 5, 6, 10].map((a) => backoffDelay(a))).toEqual([
      500, 1000, 2000, 4000, 8000, 15000, 15000, 15000,
    ]);
  });

  it('reconnects with backoff and resets the attempt counter after a successful open', () => {
    const client = createWsClient({ url: () => 'ws://test/api/ws' });
    client.ensure();
    expect(FakeWebSocket.instances).toHaveLength(1);
    FakeWebSocket.latest().close();
    vi.advanceTimersByTime(499);
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(2);
    FakeWebSocket.latest().close();
    vi.advanceTimersByTime(999);
    expect(FakeWebSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(3);
    FakeWebSocket.latest().serverOpen();
    FakeWebSocket.latest().close();
    vi.advanceTimersByTime(500);
    expect(FakeWebSocket.instances).toHaveLength(4);
    client.close();
  });

  it('queues outgoing frames until open and flushes after notifying open listeners', () => {
    const client = createWsClient({ url: () => 'ws://test/api/ws' });
    client.onConnection((state) => {
      if (state === 'open') client.send({ type: 'subscribe', projectId: P1, sinceMessageId: 7 });
    });
    client.send({ type: 'interrupt', projectId: P1, runId: null });
    const ws = FakeWebSocket.latest();
    expect(ws.sent).toEqual([]);
    ws.serverOpen();
    expect(ws.sentFrames().map((f) => f.type)).toEqual(['subscribe', 'interrupt']);
    client.close();
  });

  it('rejects invalid outgoing frames', () => {
    const client = createWsClient({ url: () => 'ws://test/api/ws' });
    expect(() => client.send({ type: 'subscribe', projectId: '', sinceMessageId: 0 })).toThrow();
    client.close();
  });

  it('validates incoming frames: invalid ones are dropped with console.warn', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const client = createWsClient({ url: () => 'ws://test/api/ws' });
    const got: unknown[] = [];
    client.onFrame((f) => got.push(f));
    client.ensure();
    const ws = FakeWebSocket.latest();
    ws.serverOpen();
    ws.serverSend('not json');
    ws.serverSend({ type: 'run-started', runId: 'r1' }); // projectId missing
    ws.serverSend(runStarted);
    expect(got).toEqual([runStarted]);
    expect(warn).toHaveBeenCalledTimes(2);
    client.close();
  });

  it('sends heartbeat pings and drops a socket that stops answering', () => {
    const client = createWsClient({ url: () => 'ws://test/api/ws', heartbeatIntervalMs: 25_000, pongTimeoutMs: 10_000 });
    client.ensure();
    const ws = FakeWebSocket.latest();
    ws.serverOpen();
    vi.advanceTimersByTime(25_000);
    expect(ws.sentFrames()).toContainEqual({ type: 'ping', projectId: null });
    ws.serverSend({ type: 'pong', projectId: null });
    vi.advanceTimersByTime(10_000);
    expect(ws.readyState).toBe(FakeWebSocket.OPEN);
    vi.advanceTimersByTime(15_000 + 10_000); // next ping, no pong
    expect(ws.readyState).toBe(FakeWebSocket.CLOSED);
    vi.advanceTimersByTime(500);
    expect(FakeWebSocket.instances).toHaveLength(2);
    client.close();
  });

  it('a server heartbeat ping counts as liveness, is not answered and not forwarded', () => {
    const client = createWsClient({ url: () => 'ws://test/api/ws', heartbeatIntervalMs: 25_000, pongTimeoutMs: 10_000 });
    const got: unknown[] = [];
    client.onFrame((f) => got.push(f));
    client.ensure();
    const ws = FakeWebSocket.latest();
    ws.serverOpen();
    vi.advanceTimersByTime(25_000); // our ping goes out
    ws.serverSend({ type: 'ping', projectId: null }); // any frame proves the socket is alive
    vi.advanceTimersByTime(10_000);
    expect(ws.readyState).toBe(FakeWebSocket.OPEN);
    expect(ws.sentFrames()).toEqual([{ type: 'ping', projectId: null }]); // no pong reply
    expect(got).toEqual([]);
    client.close();
  });

  it('visibilitychange → visible reconnects immediately when closed and emits resumed when open', () => {
    const client = createWsClient({ url: () => 'ws://test/api/ws' });
    const states: ConnectionState[] = [];
    client.onConnection((s) => states.push(s));
    client.ensure();
    FakeWebSocket.latest().serverOpen();
    setVisibility('hidden');
    setVisibility('visible');
    expect(states).toEqual(['open', 'resumed']);
    FakeWebSocket.latest().close();
    for (let i = 0; i < 4; i++) {
      vi.advanceTimersByTime(backoffDelay(i));
      FakeWebSocket.latest().close();
    }
    const before = FakeWebSocket.instances.length;
    setVisibility('visible');
    expect(FakeWebSocket.instances.length).toBe(before + 1);
    client.close();
  });

  it('close() stops reconnecting', () => {
    const client = createWsClient({ url: () => 'ws://test/api/ws' });
    client.ensure();
    client.close();
    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  describe('terminal close codes', () => {
    it('4401 before the first frame: no reconnect, emits unauthorized; ensure() connects again', () => {
      const client = createWsClient({ url: () => 'ws://test/api/ws' });
      const states: ConnectionState[] = [];
      client.onConnection((s) => states.push(s));
      client.ensure();
      FakeWebSocket.latest().serverClose(4401);
      expect(states).toEqual(['unauthorized']);
      vi.advanceTimersByTime(60_000);
      setVisibility('visible');
      expect(FakeWebSocket.instances).toHaveLength(1);
      client.ensure();
      expect(FakeWebSocket.instances).toHaveLength(2);
      FakeWebSocket.latest().serverOpen();
      expect(states).toEqual(['unauthorized', 'open']);
      client.close();
    });

    it('4401 mid-connection (logout elsewhere): closed then unauthorized, queue dropped, no reconnect', () => {
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
      const client = createWsClient({ url: () => 'ws://test/api/ws' });
      const states: ConnectionState[] = [];
      client.onConnection((s) => states.push(s));
      client.ensure();
      const ws = FakeWebSocket.latest();
      ws.serverOpen();
      ws.serverSend(runStarted);
      ws.serverClose(4401);
      expect(states).toEqual(['open', 'closed', 'unauthorized']);
      expect(client.isOpen()).toBe(false);
      // A send while logged out must neither reconnect nor be replayed after the next login.
      client.send({ type: 'interrupt', projectId: P1, runId: null });
      expect(warn).toHaveBeenCalled();
      vi.advanceTimersByTime(60_000);
      setVisibility('visible');
      expect(FakeWebSocket.instances).toHaveLength(1);
      client.ensure();
      const next = FakeWebSocket.latest();
      expect(FakeWebSocket.instances).toHaveLength(2);
      next.serverOpen();
      expect(next.sent).toEqual([]);
      client.close();
    });

    it('4403 foreign origin: no reconnect, emits forbidden and logs an error', () => {
      const error = vi.spyOn(console, 'error').mockImplementation(() => {});
      const client = createWsClient({ url: () => 'ws://test/api/ws' });
      const states: ConnectionState[] = [];
      client.onConnection((s) => states.push(s));
      client.ensure();
      FakeWebSocket.latest().serverOpen();
      FakeWebSocket.latest().serverClose(4403);
      expect(states).toEqual(['open', 'closed', 'forbidden']);
      expect(error).toHaveBeenCalledTimes(1);
      vi.advanceTimersByTime(60_000);
      expect(FakeWebSocket.instances).toHaveLength(1);
      client.close();
    });

    it('4408 slow subscriber: normal backoff reconnect', () => {
      const client = createWsClient({ url: () => 'ws://test/api/ws' });
      const states: ConnectionState[] = [];
      client.onConnection((s) => states.push(s));
      client.ensure();
      FakeWebSocket.latest().serverOpen();
      FakeWebSocket.latest().serverClose(4408);
      expect(states).toEqual(['open', 'closed']);
      vi.advanceTimersByTime(500);
      expect(FakeWebSocket.instances).toHaveLength(2);
      FakeWebSocket.latest().serverOpen();
      expect(states).toEqual(['open', 'closed', 'open']);
      client.close();
    });
  });
});
