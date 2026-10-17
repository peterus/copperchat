import { type ReactNode, useEffect, useState } from 'react';
import { currentSubscription, isIos, isStandalone, pushSupport, subscribePush, unsubscribePush } from '@/push/push';

/** Settings › Notifications: Web Push on this device (Phase 9). */
export function NotificationsSection() {
  const [support, setSupport] = useState(() => pushSupport());
  const [subscribed, setSubscribed] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (support === 'unsupported') return;
    let alive = true;
    currentSubscription()
      .then((s) => {
        if (alive) setSubscribed(s !== null);
      })
      .catch(() => {
        if (alive) setSubscribed(false);
      });
    return () => {
      alive = false;
    };
  }, [support]);

  const run = async (fn: () => Promise<void>, next: boolean) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      setSubscribed(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      setSupport(pushSupport()); // the permission prompt may have changed it
    }
  };

  let body: ReactNode;
  if (support === 'unsupported') {
    body =
      isIos() && !isStandalone() ? (
        <p className="text-sm">
          On iPhone and iPad (iOS 16.4 or later) open the Share menu in Safari and choose <strong>Add to Home Screen</strong>,
          then open CopperChat from the Home Screen to turn on notifications.
        </p>
      ) : (
        <p className="text-sm text-muted">
          This browser does not offer Web Push here. Push needs HTTPS — for example through Cloudflare Tunnel or another
          HTTPS reverse proxy.
        </p>
      );
  } else if (support === 'denied') {
    body = (
      <p className="text-sm">
        Notifications are blocked in your browser settings for this site. Allow them there, then reload this page.
      </p>
    );
  } else {
    body = (
      <div className="space-y-2">
        <p className="text-sm text-muted">
          Get notified when a run finishes, the agent needs you, or the rate-limit window reopens — only while no CopperChat
          window is visible. Signing out turns notifications off on this device.
        </p>
        {subscribed ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => void run(unsubscribePush, false)}
            className="rounded border border-border px-3 py-1 text-sm disabled:opacity-50"
          >
            Disable notifications
          </button>
        ) : (
          <button
            type="button"
            disabled={busy || subscribed === null}
            onClick={() => void run(subscribePush, true)}
            className="rounded bg-accent px-3 py-1 text-sm text-accent-fg disabled:opacity-50"
          >
            Enable notifications
          </button>
        )}
      </div>
    );
  }

  return (
    <section className="space-y-2">
      <h2 className="font-medium">Notifications</h2>
      {body}
      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
    </section>
  );
}
