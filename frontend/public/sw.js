// CopperChat service worker — push notifications ONLY.
// No fetch handler, no caching: /api and the app always go to the network.

self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

// Only same-origin project pages are click targets, so a push payload can never
// send the user to a foreign origin.
function safeUrl(url) {
  return typeof url === 'string' && /^\/p\/[0-9a-f]{12}$/.test(url) ? url : '/';
}

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = {};
  }
  event.waitUntil(
    self.registration.showNotification(typeof data.title === 'string' ? data.title : 'CopperChat', {
      body: typeof data.body === 'string' ? data.body : '',
      icon: '/icons/icon-192.png',
      badge: '/icons/badge-72.png',
      tag: typeof data.tag === 'string' ? data.tag : undefined,
      data: { url: safeUrl(data.url) },
    }),
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = safeUrl((event.notification.data || {}).url);
  event.waitUntil(
    (async () => {
      const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
      for (const client of all) {
        if (new URL(client.url).origin === self.location.origin) {
          try {
            await client.focus();
            // navigate() rejects for windows this worker does not control (e.g. after a
            // hard reload); fall back to opening the project in a new window.
            if ('navigate' in client) await client.navigate(url);
            return;
          } catch {
            break;
          }
        }
      }
      await self.clients.openWindow(url);
    })(),
  );
});
