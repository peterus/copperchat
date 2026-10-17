import { apiDelete, apiGet, apiPost } from '@/api/http';

/** `unsupported`: no service worker / PushManager / Notification here (plain http, iOS outside
 * the installed app); otherwise the Notification permission. */
export type PushSupport = 'unsupported' | 'default' | 'granted' | 'denied';

export function urlBase64ToUint8Array(b64url: string): Uint8Array<ArrayBuffer> {
  const b64 = (b64url + '='.repeat((4 - (b64url.length % 4)) % 4)).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(b64);
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

export function isIos(ua: string = navigator.userAgent): boolean {
  // iPadOS reports itself as a Mac; touch support tells them apart.
  return /iPad|iPhone|iPod/.test(ua) || (ua.includes('Macintosh') && 'ontouchend' in globalThis);
}

export function isStandalone(): boolean {
  return (
    window.matchMedia('(display-mode: standalone)').matches ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true
  );
}

export function pushSupport(w: Window = window): PushSupport {
  if (!w.navigator || !('serviceWorker' in w.navigator) || !('PushManager' in w) || !('Notification' in w)) {
    return 'unsupported';
  }
  return (w as Window & { Notification: typeof Notification }).Notification.permission as PushSupport;
}

/** Registers /sw.js (push only, never caches). Without HTTPS the browser refuses; that is fine. */
export async function registerServiceWorker(): Promise<void> {
  if (!('serviceWorker' in navigator)) return;
  try {
    await navigator.serviceWorker.register('/sw.js', { scope: '/' });
  } catch (err) {
    console.warn('[push] service worker registration failed', err);
  }
}

async function registration(): Promise<ServiceWorkerRegistration | undefined> {
  if (!('serviceWorker' in navigator)) return undefined;
  return navigator.serviceWorker.getRegistration('/');
}

export async function currentSubscription(): Promise<PushSubscription | null> {
  const reg = await registration();
  return reg ? reg.pushManager.getSubscription() : null;
}

export async function subscribePush(): Promise<void> {
  const permission = await Notification.requestPermission();
  if (permission !== 'granted') throw new Error('Notifications are blocked for this site.');
  const { publicKey } = await apiGet<{ publicKey: string }>('/push/vapid-public-key');
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(publicKey),
  });
  const json = sub.toJSON() as { endpoint?: string; keys?: { p256dh?: string; auth?: string } };
  if (!json.endpoint || !json.keys?.p256dh || !json.keys.auth) {
    await sub.unsubscribe();
    throw new Error('The browser returned an incomplete push subscription.');
  }
  try {
    await apiPost('/push/subscribe', { endpoint: json.endpoint, keys: { p256dh: json.keys.p256dh, auth: json.keys.auth } });
  } catch (err) {
    await sub.unsubscribe(); // never leave a browser subscription the server does not know
    throw err;
  }
}

export async function unsubscribePush(): Promise<void> {
  const sub = await currentSubscription();
  if (!sub) return;
  await sub.unsubscribe();
  await apiDelete('/push/subscribe', { endpoint: sub.endpoint });
}

/**
 * Session reset (logout, 401, WebSocket 4401): this device stops receiving notifications.
 * Only the browser side is removed — the session cookie is gone, so the server learns about
 * it from the push service's 404/410 on the next send and deletes the subscription then.
 */
export async function forgetPushSubscription(): Promise<void> {
  try {
    const sub = await currentSubscription();
    await sub?.unsubscribe();
  } catch (err) {
    console.warn('[push] could not remove the push subscription', err);
  }
}
