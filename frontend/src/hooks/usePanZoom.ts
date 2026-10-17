import { type CSSProperties, useCallback, useEffect, useRef, useState } from 'react';
import {
  doubleTap,
  IDENTITY,
  pan,
  type PanZoomView,
  pinch,
  type Pt,
  wheelFactor,
  zoomAt,
} from '@/lib/panZoom';

const TAP_SLOP_PX = 8;
const TAP_MS = 300;
const DOUBLE_TAP_MS = 350;
const DOUBLE_TAP_SLOP_PX = 30;

/**
 * Pan (one pointer), pinch (two pointers, anchored at the midpoint), wheel zoom about the cursor
 * and double tap/click (zoom in 2x, again → fit) for an element. Listeners are native so wheel
 * and touch scrolling can be cancelled (React's wheel listener is passive).
 */
export function usePanZoom() {
  const [view, setView] = useState<PanZoomView>(IDENTITY);
  const [el, setEl] = useState<HTMLElement | null>(null);
  const viewRef = useRef(view);
  viewRef.current = view;
  const travel = useRef(0);

  useEffect(() => {
    if (!el) return;
    const pointers = new Map<number, Pt>();
    let pinchStart: { view: PanZoomView; a: Pt; b: Pt } | null = null;
    let down: { at: number; p: Pt } | null = null;
    let lastTap: { at: number; p: Pt } | null = null;

    const local = (e: { clientX: number; clientY: number }): Pt => {
      const r = el.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top };
    };
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const p = local(e);
      setView((v) => zoomAt(v, wheelFactor(e.deltaY), p.x, p.y));
    };
    const onDown = (e: PointerEvent) => {
      el.setPointerCapture?.(e.pointerId);
      const p = local(e);
      pointers.set(e.pointerId, p);
      if (pointers.size === 1) {
        travel.current = 0;
        down = { at: e.timeStamp, p };
      } else {
        down = null;
      }
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()] as [Pt, Pt];
        pinchStart = { view: viewRef.current, a, b };
      }
    };
    const onMove = (e: PointerEvent) => {
      const prev = pointers.get(e.pointerId);
      if (!prev) return;
      const p = local(e);
      pointers.set(e.pointerId, p);
      travel.current += Math.hypot(p.x - prev.x, p.y - prev.y);
      if (pointers.size >= 2 && pinchStart) {
        const [a, b] = [...pointers.values()] as [Pt, Pt];
        setView(pinch(pinchStart, a, b));
      } else if (pointers.size === 1) {
        setView((v) => pan(v, p.x - prev.x, p.y - prev.y));
      }
    };
    const onUp = (e: PointerEvent) => {
      if (!pointers.delete(e.pointerId)) return;
      if (pointers.size < 2) pinchStart = null;
      if (pointers.size === 0 && down && travel.current <= TAP_SLOP_PX && e.timeStamp - down.at < TAP_MS) {
        const p = down.p;
        if (lastTap && e.timeStamp - lastTap.at < DOUBLE_TAP_MS && Math.hypot(p.x - lastTap.p.x, p.y - lastTap.p.y) < DOUBLE_TAP_SLOP_PX) {
          setView((v) => doubleTap(v, p));
          lastTap = null;
        } else {
          lastTap = { at: e.timeStamp, p };
        }
      }
      if (pointers.size === 0) down = null;
    };
    const onCancel = (e: PointerEvent) => {
      pointers.delete(e.pointerId);
      pinchStart = null;
      down = null;
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    el.addEventListener('pointerdown', onDown);
    el.addEventListener('pointermove', onMove);
    el.addEventListener('pointerup', onUp);
    el.addEventListener('pointercancel', onCancel);
    return () => {
      el.removeEventListener('wheel', onWheel);
      el.removeEventListener('pointerdown', onDown);
      el.removeEventListener('pointermove', onMove);
      el.removeEventListener('pointerup', onUp);
      el.removeEventListener('pointercancel', onCancel);
    };
  }, [el]);

  const reset = useCallback(() => setView(IDENTITY), []);
  /** Zoom about the container centre (the +/− buttons). */
  const zoomBy = useCallback(
    (factor: number) => {
      const r = el?.getBoundingClientRect();
      setView((v) => zoomAt(v, factor, (r?.width ?? 0) / 2, (r?.height ?? 0) / 2));
    },
    [el],
  );
  /** True when the last gesture moved more than a tap. */
  const moved = useCallback(() => travel.current > TAP_SLOP_PX, []);

  const style: CSSProperties = {
    transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})`,
    transformOrigin: '0 0',
  };

  return { view, ref: setEl, style, reset, zoomBy, moved };
}
