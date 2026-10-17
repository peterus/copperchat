import { useEffect, useRef } from 'react';

/** Runs `load` on mount / when it changes, and again whenever an agent run ends. */
export function useReloadAfterRun(load: () => Promise<void>, runActive: boolean): void {
  useEffect(() => {
    void load();
  }, [load]);
  const wasActive = useRef(runActive);
  useEffect(() => {
    if (wasActive.current && !runActive) void load();
    wasActive.current = runActive;
  }, [load, runActive]);
}
