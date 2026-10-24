import { useState } from 'react';
import { Lightbox } from '@/components/chat/Lightbox';
import type { PlanStepDTO } from '@/protocol/frames';

const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? '' : 's'}`;

/** Agent-authored ERC reasons can be up to 100 × 500 chars (E8b limits): the card shows a digest. */
export const JUSTIFICATIONS_SHOWN = 5;
export const JUSTIFICATION_CHARS = 200;
const clip = (s: string) => (s.length > JUSTIFICATION_CHARS ? `${s.slice(0, JUSTIFICATION_CHARS - 1)}…` : s);

export const STEP_STATUS_LABEL: Record<PlanStepDTO['status'], string> = {
  pending: 'Pending',
  running: 'Running',
  done: 'Done',
  failed: 'Failed',
  interrupted: 'Interrupted',
  paused: 'Paused',
};
/** A step paused by a mid-step revision while that revision awaits the user's approval. */
export const AWAITING_REVISION_LABEL = 'Paused – revision awaiting approval';
const STEP_STATUS_CLASS: Record<PlanStepDTO['status'], string> = {
  pending: 'text-muted',
  running: 'bg-accent/15 text-accent',
  done: 'bg-success/15 text-success',
  failed: 'bg-danger/15 text-danger',
  interrupted: 'bg-warning/15 text-warning',
  paused: 'bg-warning/15 text-warning',
};

export const checkpointPreviewUrl = (projectId: string, snapshotId: string) =>
  `/api/projects/${encodeURIComponent(projectId)}/history/${encodeURIComponent(snapshotId)}/preview`;

type Rule = NonNullable<PlanStepDTO['erc']>['rules'][number];

/** "type · sheet" of a pattern rule's match. */
const ruleLabel = (r: Rule) => [r.match.type, r.match.sheet].filter(Boolean).join(' · ');

/** Id justifications are collapsed to a count + a short list; pattern rules show what they covered. */
function Justifications({ items, rules }: { items: Array<[string, string]>; rules: Rule[] }) {
  const [open, setOpen] = useState(false);
  const total = items.length + rules.reduce((n, r) => n + r.covered, 0);
  if (items.length === 0 && rules.length === 0) return null;
  const shown = items.slice(0, JUSTIFICATIONS_SHOWN);
  return (
    <div>
      <button type="button" aria-expanded={open} onClick={() => setOpen((o) => !o)} className="text-muted underline">
        {total} justified
      </button>
      {open && (
        <ul className="mt-1 flex flex-col gap-0.5 break-words text-muted">
          {rules.slice(0, JUSTIFICATIONS_SHOWN).map((r) => (
            <li key={ruleLabel(r)}>
              {ruleLabel(r)}: {clip(r.reason)} ({plural(r.covered, 'error')})
            </li>
          ))}
          {shown.map(([id, reason]) => (
            <li key={id}>{clip(reason)}</li>
          ))}
          {items.length > shown.length && <li>+{items.length - shown.length} more</li>}
        </ul>
      )}
    </div>
  );
}

/**
 * One numbered step of the plan card (spec §7.3). `awaitingRevision`: the plan's revision awaits
 * approval (spec §9.4); a paused step then says so, with the agent's `reviseReason` (plain text).
 */
export function PlanStepRow({
  projectId,
  step,
  awaitingRevision = false,
  reviseReason = null,
}: {
  projectId: string;
  step: PlanStepDTO;
  awaitingRevision?: boolean;
  reviseReason?: string | null;
}) {
  const [preview, setPreview] = useState(false);
  const finished = step.status === 'done';
  const awaiting = step.status === 'paused' && awaitingRevision;
  // Changes so far count against the step's budget (spec §9.1); shown once the step has begun.
  const inProgress = (step.status === 'running' || step.status === 'paused' || step.status === 'interrupted') && step.used > 0;
  const erc = step.erc;
  return (
    <li className="flex gap-2 py-1.5">
      <span className="w-5 flex-none text-right font-mono text-muted">{step.idx}.</span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="min-w-0 break-words font-medium">{step.title}</span>
          <span className={`rounded px-1.5 text-xs font-medium ${STEP_STATUS_CLASS[step.status]}`}>
            {awaiting ? AWAITING_REVISION_LABEL : STEP_STATUS_LABEL[step.status]}
          </span>
        </div>
        {step.intent && <p className="break-words text-xs text-muted">{step.intent}</p>}
        {awaiting && reviseReason && <p className="line-clamp-6 whitespace-pre-wrap break-words text-xs text-muted">{`Agent's reason: ${reviseReason}`}</p>}
        <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
          {finished ? (
            <span>{plural(step.changeCount, 'change')}</span>
          ) : inProgress ? (
            <span className="text-muted">{`${step.used} of ${plural(step.expectedChanges, 'change')} used`}</span>
          ) : (
            <span className="text-muted">{plural(step.expectedChanges, 'change')} expected</span>
          )}
          {finished && step.checkpointId && (
            <button type="button" onClick={() => setPreview(true)} className="text-accent underline">
              Checkpoint
            </button>
          )}
          {erc && (
            <span className={erc.errors ? 'text-danger' : erc.warnings ? 'text-warning' : 'text-muted'}>
              {erc.errors === 0 && erc.warnings === 0
                ? 'ERC clean'
                : [erc.errors ? plural(erc.errors, 'ERC error') : '', erc.warnings ? plural(erc.warnings, 'ERC warning') : '']
                    .filter(Boolean)
                    .join(' · ')}
            </span>
          )}
        </div>
        {step.summary && <p className="mt-0.5 break-words text-xs">{step.summary}</p>}
        {step.failure && <p className="mt-0.5 break-words text-xs text-danger">{step.failure}</p>}
        {erc && (
          <div className="mt-0.5 text-xs">
            <Justifications items={Object.entries(erc.justifications)} rules={erc.rules} />
          </div>
        )}
      </div>
      {preview && step.checkpointId && (
        <Lightbox
          src={checkpointPreviewUrl(projectId, step.checkpointId)}
          alt={`Checkpoint after step ${step.idx}`}
          onClose={() => setPreview(false)}
        />
      )}
    </li>
  );
}
