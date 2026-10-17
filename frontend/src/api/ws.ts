import { ClientFrame, ServerFrame } from '@/protocol/frames';

/**
 * Connection lifecycle as seen by listeners.
 * - `open`: socket opened (resubscribe here; queued frames are flushed right after).
 * - `closed`: an open socket was lost; the client reconnects with backoff.
 * - `resumed`: page became visible again while the socket still looks open (heal state).
 * - `unauthorized`: server closed with 4401 (not logged in / logged out elsewhere).
 *   Terminal: no reconnect until `ensure()` is called again (after a new login).
 * - `forbidden`: server closed with 4403 (foreign Origin). Terminal like `unauthorized`.
 * A terminal state that ends an open socket is preceded by `closed`.
 */
export type ConnectionState = 'open' | 'closed' | 'resumed' | 'unauthorized' | 'forbidden';

export interface WsClient {
  /** Validated; queued while not open. Dropped (console.warn) after a terminal close. */
  send(frame: ClientFrame): void;
  onFrame(listener: (frame: ServerFrame) => void): () => void;
  onConnection(listener: (state: ConnectionState) => void): () => void;
  /** Connect if not connected; also re-enables connecting after close() or a terminal close. */
  ensure(): void;
  isOpen(): boolean;
  /** Stop and do not reconnect until ensure(). */
  close(): void;
}

export interface WsClientOptions {
  url?: () => string;
  heartbeatIntervalMs?: number;
  pongTimeoutMs?: number;
  baseDelayMs?: number;
  maxDelayMs?: number;
}

/** Backend close codes (backend/src/copperchat/web/ws.py). */
export const CLOSE_UNAUTHENTICATED = 4401;
export const CLOSE_FORBIDDEN_ORIGIN = 4403;

const CONNECTING = 0;
const OPEN = 1;

export function backoffDelay(attempt: number, baseMs = 500, maxMs = 15_000): number {
  return Math.min(baseMs * 2 ** attempt, maxMs);
}

function defaultUrl(): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}/api/ws`;
}

export function createWsClient(opts: WsClientOptions = {}): WsClient {
  const url = opts.url ?? defaultUrl;
  const heartbeatMs = opts.heartbeatIntervalMs ?? 25_000;
  const pongMs = opts.pongTimeoutMs ?? 10_000;
  const frameListeners = new Set<(frame: ServerFrame) => void>();
  const connListeners = new Set<(state: ConnectionState) => void>();
  const queue: ClientFrame[] = [];
  let socket: WebSocket | null = null;
  let attempt = 0;
  let enabled = false;
  /** Set by a 4401/4403 close; only ensure() clears it. */
  let terminal = false;
  let wasOpen = false;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  let pongTimer: ReturnType<typeof setTimeout> | null = null;
  let visibilityInstalled = false;

  /** One throwing listener must not starve the others (or abort the open flush / reconnect). */
  function safeCall<T>(listener: (arg: T) => void, arg: T, what: string): void {
    try {
      listener(arg);
    } catch (e) {
      console.error(`[ws] ${what} listener failed`, e);
    }
  }

  const emitConnection = (state: ConnectionState) => {
    for (const l of [...connListeners]) safeCall(l, state, 'connection');
  };

  function clearHeartbeat() {
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    if (pongTimer) clearTimeout(pongTimer);
    heartbeatTimer = null;
    pongTimer = null;
  }

  function clearReconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  function probe(ws: WebSocket) {
    if (ws.readyState !== OPEN || pongTimer) return;
    ws.send(JSON.stringify({ type: 'ping', projectId: null }));
    pongTimer = setTimeout(() => {
      pongTimer = null;
      // Dead connection: a browser may take long to fire onclose, so handle it now.
      handleClose(ws, 1006);
      ws.close();
    }, pongMs);
  }

  function markAlive() {
    if (pongTimer) clearTimeout(pongTimer);
    pongTimer = null;
  }

  function scheduleReconnect() {
    if (!enabled || reconnectTimer) return;
    const delay = backoffDelay(attempt, opts.baseDelayMs, opts.maxDelayMs);
    attempt += 1;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  function handleClose(ws: WebSocket, code: number) {
    if (socket !== ws) return;
    socket = null;
    clearHeartbeat();
    if (wasOpen) {
      wasOpen = false;
      emitConnection('closed');
    }
    if (code === CLOSE_UNAUTHENTICATED || code === CLOSE_FORBIDDEN_ORIGIN) {
      enabled = false;
      terminal = true;
      clearReconnect();
      queue.length = 0;
      if (code === CLOSE_FORBIDDEN_ORIGIN) {
        console.error('[ws] connection refused: Origin does not match the server (close code 4403)');
        emitConnection('forbidden');
      } else {
        emitConnection('unauthorized');
      }
      return;
    }
    scheduleReconnect();
  }

  function connect() {
    if (!enabled) return;
    if (socket && (socket.readyState === OPEN || socket.readyState === CONNECTING)) return;
    clearReconnect();
    const ws = new WebSocket(url());
    socket = ws;
    ws.onopen = () => {
      if (socket !== ws) return;
      attempt = 0;
      wasOpen = true;
      heartbeatTimer = setInterval(() => probe(ws), heartbeatMs);
      emitConnection('open'); // listeners resubscribe first …
      while (queue.length > 0 && socket === ws && ws.readyState === OPEN) {
        ws.send(JSON.stringify(queue.shift())); // … then queued frames go out
      }
    };
    ws.onmessage = (event: MessageEvent) => {
      if (socket !== ws) return;
      markAlive(); // any frame (incl. the server's heartbeat ping) proves liveness
      let raw: unknown;
      try {
        raw = JSON.parse(String(event.data));
      } catch {
        console.warn('[ws] dropped non-JSON frame');
        return;
      }
      const parsed = ServerFrame.safeParse(raw);
      if (!parsed.success) {
        console.warn('[ws] dropped invalid frame', parsed.error.issues, raw);
        return;
      }
      if (parsed.data.type === 'pong' || parsed.data.type === 'ping') return;
      for (const l of [...frameListeners]) safeCall(l, parsed.data, 'frame');
    };
    ws.onclose = (event: CloseEvent) => handleClose(ws, event.code);
    ws.onerror = () => {
      // onclose follows and schedules the reconnect.
    };
  }

  function onVisibility() {
    if (document.visibilityState !== 'visible' || !enabled) return;
    if (socket && socket.readyState === OPEN) {
      // iOS keeps zombie sockets after backgrounding: heal state and probe liveness.
      emitConnection('resumed');
      probe(socket);
      return;
    }
    attempt = 0;
    clearReconnect();
    connect();
  }

  const client: WsClient = {
    send(frame) {
      const valid = ClientFrame.parse(frame);
      if (socket && socket.readyState === OPEN) {
        socket.send(JSON.stringify(valid));
        return;
      }
      if (terminal) {
        console.warn('[ws] dropped outgoing frame: connection was refused by the server', valid.type);
        return;
      }
      queue.push(valid);
      client.ensure();
    },
    onFrame(listener) {
      frameListeners.add(listener);
      return () => frameListeners.delete(listener);
    },
    onConnection(listener) {
      connListeners.add(listener);
      return () => connListeners.delete(listener);
    },
    ensure() {
      enabled = true;
      if (terminal) {
        terminal = false;
        attempt = 0;
      }
      if (!visibilityInstalled) {
        document.addEventListener('visibilitychange', onVisibility);
        visibilityInstalled = true;
      }
      connect();
    },
    isOpen() {
      return socket !== null && socket.readyState === OPEN;
    },
    close() {
      enabled = false;
      clearReconnect();
      clearHeartbeat();
      queue.length = 0;
      const s = socket;
      socket = null;
      wasOpen = false;
      s?.close();
      if (visibilityInstalled) {
        document.removeEventListener('visibilitychange', onVisibility);
        visibilityInstalled = false;
      }
    },
  };
  return client;
}

export const wsClient: WsClient = createWsClient();
