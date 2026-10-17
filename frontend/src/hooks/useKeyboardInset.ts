import { useEffect, useState } from 'react';

export function computeKeyboardInset(innerHeight: number, vv: { height: number; offsetTop: number } | null): number {
  if (!vv) return 0;
  return Math.max(0, Math.round(innerHeight - vv.height - vv.offsetTop));
}

/** Height covered by the on-screen keyboard (iOS Safari does not resize the layout viewport). */
export function useKeyboardInset(): number {
  const [inset, setInset] = useState(0);
  useEffect(() => {
    const vv = window.visualViewport;
    if (!vv) return;
    const update = () => setInset(computeKeyboardInset(window.innerHeight, vv));
    update();
    vv.addEventListener('resize', update);
    vv.addEventListener('scroll', update);
    return () => {
      vv.removeEventListener('resize', update);
      vv.removeEventListener('scroll', update);
    };
  }, []);
  return inset;
}
