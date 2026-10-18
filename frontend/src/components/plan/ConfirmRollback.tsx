import { useEffect, useId, useState } from 'react';
import type { PlanDTO } from '@/protocol/frames';

export interface ConfirmRollbackProps {
  plan: PlanDTO;
  onConfirm: (toStep: number) => void;
  onCancel: () => void;
}

/**
 * Modal choice of the checkpoint to roll a paused plan back to: the end of a finished step, or
 * the start (the snapshot before the plan). Nothing is restored without an explicit click.
 */
export function ConfirmRollback({ plan, onConfirm, onCancel }: ConfirmRollbackProps) {
  const titleId = useId();
  const done = plan.steps.filter((s) => s.status === 'done');
  const [toStep, setToStep] = useState<number>(done.at(-1)?.idx ?? 0);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  const options = [
    { value: 0, label: 'Start: the design before the plan' },
    ...done.map((s) => ({ value: s.idx, label: `After step ${s.idx}: ${s.title}` })),
  ];
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
    >
      <div className="w-full max-w-sm rounded border border-border bg-surface p-4 text-left text-fg shadow-lg">
        <h2 id={titleId} className="mb-2 font-semibold">
          Roll back the plan?
        </h2>
        <p className="mb-3 text-sm">
          The design is restored to the chosen checkpoint and KiCad reloads it. Later steps become pending again.
        </p>
        <fieldset className="mb-4 flex max-h-64 flex-col gap-1.5 overflow-y-auto text-sm">
          <legend className="sr-only">Checkpoint</legend>
          {options.map((o) => (
            <label key={o.value} className="flex items-start gap-2">
              <input
                type="radio"
                name="rollback-step"
                value={o.value}
                checked={toStep === o.value}
                onChange={() => setToStep(o.value)}
                className="mt-1"
              />
              <span className="min-w-0 break-words">{o.label}</span>
            </label>
          ))}
        </fieldset>
        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button type="button" onClick={onCancel} className="w-full rounded border border-border px-3 py-1 sm:w-auto">
            Cancel
          </button>
          <button
            type="button"
            onClick={() => onConfirm(toStep)}
            className="w-full rounded bg-danger px-3 py-1 text-white sm:w-auto"
          >
            Roll back
          </button>
        </div>
      </div>
    </div>
  );
}
