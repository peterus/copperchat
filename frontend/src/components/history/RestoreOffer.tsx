import { useState } from 'react';
import { ConfirmRestore } from './ConfirmRestore';

/**
 * Button under a crash-recovery notice (`metadata.action === "restore"`, Phase 7); `checkpoint`:
 * the snapshot is a plan step checkpoint (E8a), restored to the state at the end of that step.
 */
export function RestoreOffer({
  projectId,
  conversationId,
  snapshotId,
  checkpoint = false,
}: {
  projectId: string;
  /** The conversation of the notice: the restore is done from it. */
  conversationId: string;
  snapshotId: string;
  checkpoint?: boolean;
}) {
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
        {checkpoint ? 'Restore step checkpoint' : 'Restore snapshot'}
      </button>
      {error && (
        <span role="alert" className="ml-2 text-danger">
          {error}
        </span>
      )}
      {state === 'confirm' && (
        <ConfirmRestore
          projectId={projectId}
          conversationId={conversationId}
          snapshotId={snapshotId}
          label={checkpoint ? 'the last finished plan step' : 'this turn'}
          checkpoint={checkpoint}
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
