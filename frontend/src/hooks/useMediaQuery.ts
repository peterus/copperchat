import { useCallback, useMemo, useSyncExternalStore } from 'react';

export function useMediaQuery(query: string): boolean {
  // One MediaQueryList per query: getSnapshot runs on every render and store check.
  const mql = useMemo(() => window.matchMedia(query), [query]);
  const subscribe = useCallback(
    (onChange: () => void) => {
      mql.addEventListener('change', onChange);
      return () => mql.removeEventListener('change', onChange);
    },
    [mql],
  );
  return useSyncExternalStore(subscribe, () => mql.matches, () => false);
}
