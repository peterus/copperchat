/** Minimal browser-WebSocket double. Install with vi.stubGlobal('WebSocket', FakeWebSocket). */
export class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readyState = FakeWebSocket.CONNECTING;
  sent: string[] = [];
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;

  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }

  static latest(): FakeWebSocket {
    const ws = FakeWebSocket.instances.at(-1);
    if (!ws) throw new Error('no FakeWebSocket created');
    return ws;
  }

  send(data: string): void {
    if (this.readyState !== FakeWebSocket.OPEN) throw new Error('send on non-open socket');
    this.sent.push(data);
  }

  /** Client- or network-initiated close (default code 1006 like a dropped connection). */
  close(code = 1006, reason = ''): void {
    if (this.readyState === FakeWebSocket.CLOSED) return;
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({ type: 'close', code, reason, wasClean: code !== 1006 } as CloseEvent);
  }

  sentFrames(): Array<Record<string, unknown>> {
    return this.sent.map((s) => JSON.parse(s) as Record<string, unknown>);
  }

  serverOpen(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.(new Event('open'));
  }

  serverSend(frame: unknown): void {
    const data = typeof frame === 'string' ? frame : JSON.stringify(frame);
    this.onmessage?.(new MessageEvent('message', { data }));
  }

  /** Server closes the connection with an application close code (e.g. 4401, 4403, 4408). */
  serverClose(code: number, reason = ''): void {
    this.close(code, reason);
  }
}
