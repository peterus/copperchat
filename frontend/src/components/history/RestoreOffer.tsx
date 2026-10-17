import { useState } from 'react';
import { ConfirmRestore } from './ConfirmRestore';

/** Button under a crash-recovery notice (`metadata.action === "restore"`, Phase 7). */
export function RestoreOffer({ projectId, snapshotId }: { projectId: string; snapshotId: string }) {
  const [state, setState] = useState<'idle' | 'confirm' | 'done'>('idle');
  const [error, setError] = useState<string | null>(null);
  if (state === 'done') return <span className="text-success">Restored.</span>;
  return (
    <>
      <button
        type="button"
        className="ml-2 rounded border border-border px-2 py-0.5 text-fg"
        onClick={() => {
          setError(null);
          setState('confirm');
        }}
      >
        Restore snapshot
      </button>
      {error && (
        <span role="alert" className="ml-2 text-danger">
          {error}
        </span>
      )}
      {state === 'confirm' && (
        <ConfirmRestore
          projectId={projectId}
          snapshotId={snapshotId}
          label="this turn"
          onDone={() => setState('done')}
          onCancel={() => setState('idle')}
          onError={(msg) => {
            setError(msg);
            setState('idle');
          }}
        />
      )}
    </>
  );
}
