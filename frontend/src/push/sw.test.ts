import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it, vi } from 'vitest';

const src = readFileSync(path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../public/sw.js'), 'utf8');

type Handler = (e: unknown) => void;

function loadSw() {
  const handlers: Partial<Record<string, Handler>> = {};
  const client = { url: 'https://cc.example/projects', focus: vi.fn(), navigate: vi.fn() };
  client.navigate.mockImplementation(async () => client);
  const self = {
    location: { origin: 'https://cc.example' },
    addEventListener: (t: string, h: Handler) => {
      handlers[t] = h;
    },
    skipWaiting: vi.fn(),
    clients: { claim: vi.fn(), matchAll: vi.fn().mockResolvedValue([client]), openWindow: vi.fn() },
    registration: { showNotification: vi.fn() },
  };
  new Function('self', src)(self);
  return { handlers, self, client };
}

function fire(handler: Handler | undefined, event: Record<string, unknown>): Promise<unknown> {
  let p: Promise<unknown> = Promise.resolve();
  if (!handler) throw new Error('handler not registered');
  handler({ ...event, waitUntil: (x: Promise<unknown>) => (p = x) });
  return p;
}

describe('sw.js', () => {
  it('never registers a fetch handler (no caching, /api untouched)', () => {
    const { handlers } = loadSw();
    expect(handlers.fetch).toBeUndefined();
    expect(src).not.toMatch(/caches\./);
  });

  it('shows a notification from the push payload', async () => {
    const { handlers, self } = loadSw();
    const payload = { title: 'T', body: 'B', url: '/p/a1b2c3d4e5f6', tag: 'run-a1b2c3d4e5f6' };
    await fire(handlers.push, { data: { json: () => payload } });
    expect(self.registration.showNotification).toHaveBeenCalledWith(
      'T',
      expect.objectContaining({ body: 'B', tag: 'run-a1b2c3d4e5f6', data: { url: '/p/a1b2c3d4e5f6' } }),
    );
  });

  it('keeps a session deep link and drops other paths', async () => {
    const { handlers, self } = loadSw();
    const deep = '/p/a1b2c3d4e5f6/s/0123456789abcdef0123456789abcdef';
    for (const url of [deep, '/p/a1b2c3d4e5f6/s/xyz', '/p/a1b2c3d4e5f6/s/0123456789abcdef0123456789abcdef/x', '/settings']) {
      await fire(handlers.push, { data: { json: () => ({ title: 'T', body: 'B', url, tag: 't' }) } });
    }
    const urls = self.registration.showNotification.mock.calls.map((c: unknown[]) => (c[1] as { data: { url: string } }).data.url);
    expect(urls).toEqual([deep, '/', '/', '/']);
  });

  it('survives a payload that is not JSON', async () => {
    const { handlers, self } = loadSw();
    await fire(handlers.push, {
      data: {
        json: () => {
          throw new SyntaxError('bad');
        },
      },
    });
    expect(self.registration.showNotification).toHaveBeenCalledWith('CopperChat', expect.objectContaining({ data: { url: '/' } }));
  });

  it('click focuses an open window and navigates to the project', async () => {
    const { handlers, client, self } = loadSw();
    await fire(handlers.notificationclick, { notification: { close: vi.fn(), data: { url: '/p/a1b2c3d4e5f6' } } });
    expect(client.navigate).toHaveBeenCalledWith('/p/a1b2c3d4e5f6');
    expect(client.focus).toHaveBeenCalled();
    expect(self.clients.openWindow).not.toHaveBeenCalled();
  });

  it('rejects foreign click targets and ignores windows of other origins', async () => {
    const { handlers, self } = loadSw();
    self.clients.matchAll.mockResolvedValue([
      { url: 'https://cc.example.evil.test/', focus: vi.fn(), navigate: vi.fn() },
    ]);
    await fire(handlers.notificationclick, { notification: { close: vi.fn(), data: { url: 'https://evil.example/' } } });
    expect(self.clients.openWindow).toHaveBeenCalledWith('/');
  });

  it('opens a new window when navigating an uncontrolled window fails', async () => {
    const { handlers, self, client } = loadSw();
    client.navigate.mockRejectedValue(new TypeError('not controlled'));
    await fire(handlers.notificationclick, { notification: { close: vi.fn(), data: { url: '/p/a1b2c3d4e5f6' } } });
    expect(self.clients.openWindow).toHaveBeenCalledWith('/p/a1b2c3d4e5f6');
    expect(client.focus).not.toHaveBeenCalled(); // focus would consume the user activation
  });
});
