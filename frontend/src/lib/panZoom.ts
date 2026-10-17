/**
 * Pan/zoom math for the agent-view image. Coordinates are CSS px relative to the container; the
 * content transform is `translate(x, y) scale(scale)` with origin `0 0`.
 */
export interface PanZoomView {
  scale: number;
  x: number;
  y: number;
}
export interface Limits {
  min: number;
  max: number;
}
export interface Pt {
  x: number;
  y: number;
}

export const IDENTITY: PanZoomView = { scale: 1, x: 0, y: 0 };
export const DEFAULT_LIMITS: Limits = { min: 1, max: 12 };
/** Zoom step of the +/− buttons and of a double tap. */
export const ZOOM_STEP = 2;

export function clampScale(s: number, l: Limits = DEFAULT_LIMITS): number {
  return Math.min(l.max, Math.max(l.min, s));
}

export function wheelFactor(deltaY: number): number {
  return Math.exp(-deltaY * 0.0015);
}

export function zoomAt(v: PanZoomView, factor: number, cx: number, cy: number, l: Limits = DEFAULT_LIMITS): PanZoomView {
  const scale = clampScale(v.scale * factor, l);
  const k = scale / v.scale;
  return { scale, x: cx - (cx - v.x) * k, y: cy - (cy - v.y) * k };
}

export function pan(v: PanZoomView, dx: number, dy: number): PanZoomView {
  return { scale: v.scale, x: v.x + dx, y: v.y + dy };
}

export function distance(a: Pt, b: Pt): number {
  return Math.hypot(b.x - a.x, b.y - a.y);
}

export function midpoint(a: Pt, b: Pt): Pt {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

export function pinch(start: { view: PanZoomView; a: Pt; b: Pt }, a: Pt, b: Pt, l: Limits = DEFAULT_LIMITS): PanZoomView {
  const d0 = distance(start.a, start.b);
  const factor = d0 === 0 ? 1 : distance(a, b) / d0;
  const scale = clampScale(start.view.scale * factor, l);
  const k = scale / start.view.scale;
  const m0 = midpoint(start.a, start.b);
  const m1 = midpoint(a, b);
  return { scale, x: m1.x - (m0.x - start.view.x) * k, y: m1.y - (m0.y - start.view.y) * k };
}

/** Double tap: zoom in about the tap; when already zoomed in, back to the fitted view. */
export function doubleTap(v: PanZoomView, p: Pt, l: Limits = DEFAULT_LIMITS): PanZoomView {
  return v.scale > l.min ? IDENTITY : zoomAt(v, ZOOM_STEP, p.x, p.y, l);
}

export function toContent(v: PanZoomView, px: number, py: number): Pt {
  return { x: (px - v.x) / v.scale, y: (py - v.y) / v.scale };
}
