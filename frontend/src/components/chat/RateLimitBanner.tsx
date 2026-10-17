import { useEffect, useState } from 'react';

/** Max delay between re-checks, so the banner also disappears when the reset lies far ahead. */
const MAX_TICK_MS = 30_000;

export function RateLimitBanner({ until }: { until: string | null }) {
  const [, tick] = useState(0);
  const resetsAt = until ? Date.parse(until) : NaN;
  const remaining = Number.isFinite(resetsAt) ? resetsAt - Date.now() : 0;
  const active = remaining > 0;
  useEffect(() => {
    if (!active) return;
    const id = setTimeout(() => tick((n) => n + 1), Math.min(MAX_TICK_MS, remaining + 50));
    return () => clearTimeout(id);
  });
  if (!active) return null;
  const at = new Date(resetsAt).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  return (
    <div role="status" className="border-b border-warning bg-warning/15 px-3 py-2 text-sm text-warning">
      Rate limit reached — queued messages resume automatically at {at}.
    </div>
  );
}
