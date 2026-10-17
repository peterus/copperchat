export interface RunControlsProps {
  runActive: boolean;
  onStop: () => void;
}

/** Stop for the active run. `runActive` is reset on every reconnect, so it cannot get stuck. */
export function RunControls({ runActive, onStop }: RunControlsProps) {
  if (!runActive) return null;
  return (
    <button
      type="button"
      onClick={onStop}
      className="shrink-0 rounded-lg border border-danger px-3 py-2 text-sm font-medium text-danger"
    >
      Stop
    </button>
  );
}
