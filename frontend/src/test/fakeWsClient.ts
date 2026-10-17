import type { ConnectionState, WsClient } from '@/api/ws';
import { ClientFrame, ServerFrame } from '@/protocol/frames';

/** In-memory WsClient for store/component tests; frames are schema-validated both ways. */
export class FakeWsClient implements WsClient {
  sent: ClientFrame[] = [];
  open = false;
  ensureCalls = 0;
  private frameListeners = new Set<(f: ServerFrame) => void>();
  private connListeners = new Set<(s: ConnectionState) => void>();

  send(frame: ClientFrame): void {
    this.sent.push(ClientFrame.parse(frame));
  }
  onFrame(listener: (f: ServerFrame) => void): () => void {
    this.frameListeners.add(listener);
    return () => this.frameListeners.delete(listener);
  }
  onConnection(listener: (s: ConnectionState) => void): () => void {
    this.connListeners.add(listener);
    return () => this.connListeners.delete(listener);
  }
  ensure(): void {
    this.ensureCalls += 1;
  }
  isOpen(): boolean {
    return this.open;
  }
  close(): void {
    this.open = false;
  }

  // --- test controls ---
  private emitState(state: ConnectionState): void {
    for (const l of [...this.connListeners]) l(state);
  }
  connect(): void {
    this.open = true;
    this.emitState('open');
  }
  drop(): void {
    this.open = false;
    this.emitState('closed');
  }
  resume(): void {
    this.emitState('resumed');
  }
  /** Terminal close like the real client: 'closed' first (if open), then the terminal state. */
  refuse(state: 'unauthorized' | 'forbidden'): void {
    if (this.open) this.drop();
    this.emitState(state);
  }
  emit(frame: unknown): void {
    const valid = ServerFrame.parse(frame);
    for (const l of [...this.frameListeners]) l(valid);
  }
  framesOfType<T extends ClientFrame['type']>(type: T): Array<Extract<ClientFrame, { type: T }>> {
    return this.sent.filter((f): f is Extract<ClientFrame, { type: T }> => f.type === type);
  }
}
