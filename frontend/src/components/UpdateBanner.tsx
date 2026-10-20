import { useEffect, useState } from 'react';
import { z } from 'zod';
import { apiUrl } from '@/api/http';
import { wsClient, type WsClient } from '@/api/ws';
import { BUILD_VERSION } from '@/lib/buildVersion';

const Health = z.object({ version: z.string() });

/**
 * Plain fetch, not apiGet: /api/health is public, and whatever answers it (a proxy, a foreign
 * server on the port) must never trigger the 401 logout handler.
 */
async function fetchHealthVersion(): Promise<string> {
  const res = await fetch(apiUrl('/health'), { credentials: 'same-origin', headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return Health.parse(await res.json()).version;
}

export interface UpdateBannerProps {
  /** Injected in tests; the app uses the shared socket. */
  ws?: WsClient;
  reload?: () => void;
}

/**
 * Old tabs after an upgrade (spec §6.2): on every WS (re)connect, compare `/api/health`
 * `version` with the build version; a mismatch offers a reload. A failed check shows nothing.
 */
export function UpdateBanner({ ws = wsClient, reload = () => window.location.reload() }: UpdateBannerProps) {
  const [serverVersion, setServerVersion] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    const off = ws.onConnection((state) => {
      if (state !== 'open') return;
      fetchHealthVersion().then(
        (version) => {
          if (live) setServerVersion(version);
        },
        (e: unknown) => console.warn('[update] health check failed', e),
      );
    });
    return () => {
      live = false;
      off();
    };
  }, [ws]);

  if (serverVersion === null || serverVersion === BUILD_VERSION) return null;
  return (
    <div
      role="alert"
      className="fixed inset-x-0 top-0 z-[60] flex items-center justify-center gap-3 bg-accent px-3 py-2 pt-[max(0.5rem,env(safe-area-inset-top))] text-sm text-white shadow"
    >
      <span>CopperChat was updated — reload</span>
      <button type="button" onClick={reload} className="rounded border border-white/70 px-2 py-0.5 font-medium">
        Reload
      </button>
    </div>
  );
}
