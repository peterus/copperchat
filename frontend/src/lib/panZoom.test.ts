import { describe, expect, it } from 'vitest';
import {
  clampScale,
  distance,
  doubleTap,
  IDENTITY,
  midpoint,
  pan,
  pinch,
  toContent,
  wheelFactor,
  zoomAt,
} from './panZoom';

describe('pan/zoom math', () => {
  it('clamps the scale', () => {
    expect(clampScale(0.5)).toBe(1);
    expect(clampScale(50)).toBe(12);
    expect(clampScale(3, { min: 0.5, max: 4 })).toBe(3);
  });

  it('wheel up zooms in, wheel down zooms out, zero is neutral', () => {
    expect(wheelFactor(-100)).toBeGreaterThan(1);
    expect(wheelFactor(100)).toBeLessThan(1);
    expect(wheelFactor(0)).toBe(1);
    expect(wheelFactor(-100) * wheelFactor(100)).toBeCloseTo(1, 10);
  });

  it('zooms about the cursor: the content point under the cursor stays put', () => {
    const v0 = { scale: 2, x: -50, y: -20 };
    const before = toContent(v0, 120, 80);
    const v1 = zoomAt(v0, 1.5, 120, 80);
    expect(v1.scale).toBe(3);
    const after = toContent(v1, 120, 80);
    expect(after.x).toBeCloseTo(before.x, 10);
    expect(after.y).toBeCloseTo(before.y, 10);
  });

  it('does not move when the scale is already at a limit', () => {
    expect(zoomAt({ scale: 12, x: -10, y: -10 }, 2, 50, 50)).toEqual({ scale: 12, x: -10, y: -10 });
  });

  it('pans by a delta', () => {
    expect(pan(IDENTITY, 5, -3)).toEqual({ scale: 1, x: 5, y: -3 });
  });

  it('pinch scales by the finger distance ratio and keeps the midpoint anchored', () => {
    const start = { view: IDENTITY, a: { x: 100, y: 100 }, b: { x: 200, y: 100 } };
    const v = pinch(start, { x: 50, y: 100 }, { x: 250, y: 100 });
    expect(v.scale).toBeCloseTo(2, 10);
    const anchor = toContent(v, 150, 100);
    expect(anchor.x).toBeCloseTo(150, 10);
    expect(anchor.y).toBeCloseTo(100, 10);
  });

  it('pinch follows a moving midpoint (two-finger pan)', () => {
    const start = { view: IDENTITY, a: { x: 0, y: 0 }, b: { x: 100, y: 0 } };
    expect(pinch(start, { x: 30, y: 40 }, { x: 130, y: 40 })).toEqual({ scale: 1, x: 30, y: 40 });
  });

  it('pinch respects limits', () => {
    const start = { view: IDENTITY, a: { x: 0, y: 0 }, b: { x: 100, y: 0 } };
    expect(pinch(start, { x: 0, y: 0 }, { x: 10, y: 0 }).scale).toBe(1);
  });

  it('double tap zooms in about the tap, and back to fit when zoomed', () => {
    const v = doubleTap(IDENTITY, { x: 40, y: 30 });
    expect(v.scale).toBe(2);
    expect(toContent(v, 40, 30)).toEqual({ x: 40, y: 30 });
    expect(doubleTap(v, { x: 10, y: 10 })).toEqual(IDENTITY);
  });

  it('distance and midpoint', () => {
    expect(distance({ x: 0, y: 0 }, { x: 3, y: 4 })).toBe(5);
    expect(midpoint({ x: 0, y: 2 }, { x: 4, y: 6 })).toEqual({ x: 2, y: 4 });
  });

  it('maps client px to content px', () => {
    expect(toContent({ scale: 2, x: 10, y: 20 }, 50, 60)).toEqual({ x: 20, y: 20 });
  });
});
