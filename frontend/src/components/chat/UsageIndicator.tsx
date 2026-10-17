import type { Usage } from '@/protocol/frames';

export function formatTokens(n: number): string {
  if (n >= 999_950) return `${+(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  if (n >= 1000) return `${+(n / 1000).toFixed(1)}k`;
  return String(n);
}

const time = (iso: string) => new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });

const LIMIT_LABEL: Record<NonNullable<Usage['rateLimit']>['status'], string> = {
  allowed: 'limit ok',
  allowed_warning: 'limit nearly reached',
  rejected: 'limit reached',
};

/** Usage of the last run (the backend's usage frame already includes sub-agent tokens). */
export function UsageIndicator({ usage }: { usage: Usage | null }) {
  if (!usage) return null;
  const input = usage.inputTokens + usage.cacheReadTokens + usage.cacheCreationTokens;
  const parts = [`in ${formatTokens(input)}`, `out ${formatTokens(usage.outputTokens)}`];
  if (usage.costUsd !== undefined) parts.push(`$${usage.costUsd.toFixed(2)}`);
  const rl = usage.rateLimit;
  if (rl && rl.status !== 'allowed') {
    parts.push(rl.resetsAt ? `${LIMIT_LABEL[rl.status]}, resets ${time(rl.resetsAt)}` : LIMIT_LABEL[rl.status]);
  }
  return (
    <p aria-label="Token usage" className="px-3 pb-1 text-right text-[11px] text-muted">
      {parts.join(' · ')}
    </p>
  );
}
