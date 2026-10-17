import { afterEach, describe, expect, it, vi } from 'vitest';
import * as http from '@/api/http';
import {
  forgetPushSubscription,
  isIos,
  isStandalone,
  pushSupport,
  subscribePush,
  unsubscribePush,
  urlBase64ToUint8Array,
} from './push';

afterEach(() => vi.restoreAllMocks());

function fakeSub(endpoint = 'https://push.example/x') {
  return {
    endpoint,
    toJSON: () => ({ endpoint, keys: { p256dh: 'p', auth: 'a' } }),
    unsubscribe: vi.fn().mockResolvedValue(true),
  };
}

function stubBrowser(sub: ReturnType<typeof fakeSub> | null, permission = 'granted') {
  const subscribe = vi.fn().mockResolvedValue(sub);
  const reg = { pushManager: { subscribe, getSubscription: vi.fn().mockResolvedValue(sub) } };
  vi.stubGlobal('navigator', {
    userAgent: 'test',
    serviceWorker: { ready: Promise.resolve(reg), getRegistration: vi.fn().mockResolvedValue(reg) },
  });
  vi.stubGlobal('Notification', { requestPermission: vi.fn().mockResolvedValue(permission) });
  return { subscribe };
}

describe('push helpers', () => {
  it('decodes base64url VAPID keys', () => {
    expect(Array.from(urlBase64ToUint8Array('AQID-_8'))).toEqual([1, 2, 3, 251, 255]);
  });

  it('detects iOS and standalone', () => {
    expect(isIos('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)')).toBe(true);
    expect(isIos('Mozilla/5.0 (X11; Linux x86_64)')).toBe(false);
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: q === '(display-mode: standalone)' }));
    expect(isStandalone()).toBe(true);
  });

  it('reports unsupported without PushManager', () => {
    expect(pushSupport({ navigator: {} } as Window)).toBe('unsupported');
    expect(pushSupport({} as Window)).toBe('unsupported');
  });

  it('reports the permission when supported', () => {
    const w = { navigator: { serviceWorker: {} }, PushManager: {}, Notification: { permission: 'denied' } };
    expect(pushSupport(w as unknown as Window)).toBe('denied');
  });

  it('subscribes with the server key and posts the subscription; unsubscribe deletes it', async () => {
    const sub = fakeSub();
    const { subscribe } = stubBrowser(sub);
    vi.spyOn(http, 'apiGet').mockResolvedValue({ publicKey: 'AQID' });
    const post = vi.spyOn(http, 'apiPost').mockResolvedValue(undefined);
    const del = vi.spyOn(http, 'apiDelete').mockResolvedValue(undefined);
    await subscribePush();
    expect(subscribe).toHaveBeenCalledWith({ userVisibleOnly: true, applicationServerKey: new Uint8Array([1, 2, 3]) });
    expect(post).toHaveBeenCalledWith('/push/subscribe', { endpoint: 'https://push.example/x', keys: { p256dh: 'p', auth: 'a' } });
    await unsubscribePush();
    expect(sub.unsubscribe).toHaveBeenCalled();
    expect(del).toHaveBeenCalledWith('/push/subscribe', { endpoint: 'https://push.example/x' });
  });

  it('drops the browser subscription when the server refuses it', async () => {
    const sub = fakeSub();
    stubBrowser(sub);
    vi.spyOn(http, 'apiGet').mockResolvedValue({ publicKey: 'AQID' });
    vi.spyOn(http, 'apiPost').mockRejectedValue(new Error('push endpoint refused'));
    await expect(subscribePush()).rejects.toThrow(/refused/);
    expect(sub.unsubscribe).toHaveBeenCalled();
  });

  it('throws when permission is denied', async () => {
    stubBrowser(null, 'denied');
    await expect(subscribePush()).rejects.toThrow(/blocked/);
  });

  it('forgets the local subscription on session reset without calling the server', async () => {
    const sub = fakeSub();
    stubBrowser(sub);
    const del = vi.spyOn(http, 'apiDelete');
    await forgetPushSubscription();
    expect(sub.unsubscribe).toHaveBeenCalled();
    expect(del).not.toHaveBeenCalled();
  });

  it('forgetting is a no-op without service workers', async () => {
    vi.stubGlobal('navigator', { userAgent: 'test' });
    await expect(forgetPushSubscription()).resolves.toBeUndefined();
  });
});
