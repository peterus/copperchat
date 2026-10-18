import { ListChecks } from 'lucide-react';
import { useContext, useEffect, useState } from 'react';
import { ChatStoreContext } from '@/hooks/useChat';
import type { PlanDTO } from '@/protocol/frames';
import { usePlans, usePlansStore } from '@/stores/plans';
import { ConfirmRollback } from './ConfirmRollback';
import { PlanStepRow } from './PlanStepRow';

const STATUS_LABEL: Record<PlanDTO['status'], string> = {
  proposed: 'Proposed',
  approved: 'Approved',
  running: 'Running',
  paused: 'Paused',
  done: 'Done',
  rejected: 'Rejected',
  aborted: 'Aborted',
};
const STATUS_CLASS: Record<PlanDTO['status'], string> = {
  proposed: 'bg-accent/15 text-accent',
  approved: 'bg-accent/15 text-accent',
  running: 'bg-accent/15 text-accent',
  paused: 'bg-warning/15 text-warning',
  done: 'bg-success/15 text-success',
  rejected: 'text-muted',
  aborted: 'text-muted',
};

/** Pause reasons of E8b (as built), worded for the user. */
const PAUSE_REASON: Record<string, string> = {
  turn_ended: 'the turn ended before the plan was finished',
  interrupted: 'it was stopped',
  rate_limited: 'the usage limit was reached',
  kicad_failed: 'KiCad stopped responding',
  document_inconsistent: 'the design did not match what the agent wrote',
  step_exceeded: 'a step made more changes than planned',
  erc_regression: 'a step left new ERC errors',
  external_change: 'the design files were changed outside CopperChat',
  server_restart: 'the server restarted',
  rolled_back: 'it was rolled back',
};

/** Full width on phones (the card sits in the chat stream), natural width from `sm` up. */
const BTN = 'w-full rounded px-3 py-1.5 text-sm disabled:opacity-50 sm:w-auto';
const PRIMARY = `${BTN} bg-accent text-accent-fg`;
const SECONDARY = `${BTN} border border-border`;
const DANGER = `${BTN} border border-danger text-danger`;

/** Final actions (Abort, Reject): the first tap arms the button ("Really …?"), the second acts. */
export const ARM_MS = 4000;
function ArmedButton({
  label,
  disabled,
  className,
  onConfirm,
}: {
  label: string;
  disabled: boolean;
  className: string;
  onConfirm: () => void;
}) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), ARM_MS);
    return () => clearTimeout(t);
  }, [armed]);
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => {
        if (!armed) return setArmed(true);
        setArmed(false);
        onConfirm();
      }}
      className={armed ? `${BTN} bg-danger text-white` : className}
    >
      {armed ? `Really ${label.toLowerCase()}?` : label}
    </button>
  );
}

/**
 * The plan card in the chat (spec §7.3): title, summary, numbered steps with their status and,
 * once finished, change count, checkpoint and ERC; buttons by plan state. Rendered for notice
 * rows with `metadata.action === 'plan'`; follows `plan-updated` frames through the plans store.
 */
export function PlanCard({ projectId, planId }: { projectId: string; planId: string }) {
  const store = usePlansStore();
  const plan = usePlans((s) => (s.projectId === projectId ? s.byId[planId] : undefined));
  const pending = usePlans((s) => s.pending[planId]);
  const error = usePlans((s) => s.errors[planId]);
  const chat = useContext(ChatStoreContext);
  const runActive = chat((s) => s.chats[projectId]?.runActive ?? false);
  const [confirm, setConfirm] = useState(false);

  useEffect(() => {
    void store.getState().ensure(projectId, planId);
  }, [store, projectId, planId]);

  if (!plan) {
    return (
      <div className="rounded border border-border bg-surface p-3 text-left text-sm">
        {error ? (
          <p role="alert" className="text-danger">
            {error.message}
          </p>
        ) : (
          <p className="text-muted">Loading the plan…</p>
        )}
      </div>
    );
  }

  const s = store.getState();
  const busy = pending !== undefined;
  // A new revision (or another plan) disarms a pending "Really …?", also when the status stays.
  const armKey = `${plan.id}:${plan.revision}`;
  const reason = plan.pauseReason ? (PAUSE_REASON[plan.pauseReason] ?? plan.pauseReason.replaceAll('_', ' ')) : null;
  return (
    <section
      aria-labelledby={`plan-${plan.id}-title`}
      className="w-full rounded-lg border border-border bg-surface p-3 text-left text-sm text-fg"
    >
      <header className="flex flex-wrap items-center gap-2">
        <ListChecks aria-hidden className="h-4 w-4 flex-none text-muted" />
        <h3 id={`plan-${plan.id}-title`} className="min-w-0 flex-1 break-words font-semibold">
          {plan.title}
        </h3>
        {plan.revision > 1 && <span className="rounded bg-surface-2 px-1.5 text-xs">Revision {plan.revision}</span>}
        <span data-plan-status className={`rounded px-1.5 text-xs font-medium ${STATUS_CLASS[plan.status]}`}>
          {STATUS_LABEL[plan.status]}
        </span>
      </header>
      {plan.summary && <p className="mt-1 whitespace-pre-wrap break-words">{plan.summary}</p>}
      {plan.status === 'paused' && reason && <p className="mt-1 text-xs text-warning">Paused because {reason}.</p>}
      {plan.status === 'approved' && <p className="mt-1 text-xs text-muted">Waiting for the agent to start.</p>}
      <ol className="mt-2 divide-y divide-border">
        {plan.steps.map((st) => (
          <PlanStepRow key={st.idx} projectId={projectId} step={st} />
        ))}
      </ol>
      {error && (
        <p role="alert" className="mt-2 rounded bg-danger/10 p-2 text-xs text-danger">
          {error.message}
        </p>
      )}
      {plan.status === 'proposed' && (
        <div className="mt-2 flex flex-col gap-2 sm:flex-row">
          <button type="button" disabled={busy} onClick={() => void s.approve(plan.id)} className={PRIMARY}>
            {pending === 'approve' ? 'Approving…' : 'Approve'}
          </button>
          <ArmedButton key={armKey} label="Reject" disabled={busy} className={SECONDARY} onConfirm={() => void s.reject(plan.id)} />
        </div>
      )}
      {plan.status === 'approved' && (
        <div className="mt-2 flex flex-col gap-2 sm:flex-row">
          <ArmedButton key={armKey} label="Abort" disabled={busy} className={DANGER} onConfirm={() => void s.abort(plan.id)} />
        </div>
      )}
      {plan.status === 'running' && (
        <div className="mt-2 flex flex-col gap-2 sm:flex-row">
          <button
            type="button"
            disabled={!runActive}
            onClick={() => chat.getState().stop(projectId)}
            className={DANGER}
          >
            Stop
          </button>
        </div>
      )}
      {plan.status === 'paused' && (
        <div className="mt-2 flex flex-col gap-2 sm:flex-row">
          <button type="button" disabled={busy} onClick={() => void s.resume(plan.id)} className={PRIMARY}>
            {pending === 'resume' ? 'Resuming…' : 'Resume'}
          </button>
          <button type="button" disabled={busy} onClick={() => setConfirm(true)} className={SECONDARY}>
            {pending === 'rollback' ? 'Rolling back…' : 'Roll back to…'}
          </button>
          <ArmedButton key={armKey} label="Abort" disabled={busy} className={DANGER} onConfirm={() => void s.abort(plan.id)} />
        </div>
      )}
      {confirm && (
        <ConfirmRollback
          plan={plan}
          onCancel={() => setConfirm(false)}
          onConfirm={(toStep) => {
            setConfirm(false);
            void s.rollback(plan.id, toStep);
          }}
        />
      )}
    </section>
  );
}
