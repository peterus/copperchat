import { useEffect, useId, useState } from 'react';
import { errorText, restoreSnapshot } from './restore';

export interface ConfirmRestoreProps {
  projectId: string;
  snapshotId: string;
  /** The turn the snapshot was taken before, e.g. its label (shown in quotes). */
  label: string;
  /** ISO time of the snapshot, if known. */
  createdAt?: string;
  onDone: () => void;
  onCancel: () => void;
  onError: (message: string) => void;
}

/** Modal confirmation before an undo: nothing is restored without an explicit click. */
export function ConfirmRestore({
  projectId,
  snapshotId,
  label,
  createdAt,
  onDone,
  onCancel,
  onError,
}: ConfirmRestoreProps) {
  const titleId = useId();
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [busy, onCancel]);

  async function restore() {
    setBusy(true);
    try {
      await restoreSnapshot(projectId, snapshotId);
      onDone();
    } catch (e) {
      onError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
    >
      <div className="w-full max-w-sm rounded border border-border bg-surface p-4 text-fg shadow-lg">
        <h2 id={titleId} className="mb-2 font-semibold">
          Restore snapshot?
        </h2>
        <p className="mb-4 text-sm">
          Restore the design to its state before “{label}”
          {createdAt ? ` (${new Date(createdAt).toLocaleString()})` : ''}. Changes made after it are replaced and
          KiCad reloads the restored files.
        </p>
        <div className="flex justify-end gap-2">
          <button type="button" onClick={onCancel} disabled={busy} className="rounded border border-border px-3 py-1">
            Cancel
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => void restore()}
            className="rounded bg-danger px-3 py-1 text-white disabled:opacity-50"
          >
            {busy ? 'Restoring…' : 'Restore'}
          </button>
        </div>
      </div>
    </div>
  );
}
