import { useState } from 'react';
import type { MessageDTO } from '@/protocol/frames';
import { Markdown } from './Markdown';
import { describeStep, subagentTitle, type SubagentGroup } from './subagent';

export type SubagentStatus = 'running' | 'done' | 'failed' | 'stopped';

export interface SubagentCardProps {
  toolUse: MessageDTO;
  group: SubagentGroup;
  runActive: boolean;
}

export function SubagentCard({ toolUse, group, runActive }: SubagentCardProps) {
  const status: SubagentStatus = group.result
    ? group.result.metadata.isError === true
      ? 'failed'
      : 'done'
    : runActive
      ? 'running'
      : 'stopped';
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const open = userOpen ?? status === 'running';
  const lines = group.steps
    .map((m) => [m.id, describeStep(m)] as const)
    .filter((e): e is readonly [number, string] => e[1] !== null);
  return (
    <details
      open={open}
      data-role="assistant"
      data-subagent={String(toolUse.metadata.toolUseId ?? '')}
      className="rounded border border-border bg-surface px-3 py-2 text-sm"
    >
      {/* Controlled via the summary click only (no onToggle: the async toggle event from
          the initial auto-open would otherwise pin the card open after it finished). */}
      <summary
        className="flex cursor-pointer select-none items-center gap-2"
        onClick={(e) => {
          e.preventDefault();
          setUserOpen(!open);
        }}
      >
        <span className="font-medium">{subagentTitle(toolUse)}</span>
        <span
          className={`rounded px-1.5 text-xs ${status === 'failed' ? 'bg-danger/15 text-danger' : 'bg-fg/10 text-muted'}`}
        >
          {status}
        </span>
        {status === 'running' && (
          <span aria-label="working" className="inline-block h-2 w-2 animate-pulse rounded-full bg-accent" />
        )}
        <span className="ml-auto text-xs text-muted">
          {lines.length} {lines.length === 1 ? 'step' : 'steps'}
        </span>
      </summary>
      <ol className="mt-2 space-y-1 border-l border-border pl-3 text-xs text-muted">
        {lines.map(([id, text]) => (
          <li key={id} className="break-words">
            {text}
          </li>
        ))}
      </ol>
      {group.result && (
        <div className="mt-2 border-t border-border pt-2">
          <p className="mb-1 text-xs font-medium text-muted">Summary</p>
          <Markdown text={group.result.content} />
        </div>
      )}
    </details>
  );
}
