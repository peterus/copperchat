import { act, renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { setMedia } from '@/test/media';
import { useMediaQuery } from './useMediaQuery';

describe('useMediaQuery', () => {
  it('creates one MediaQueryList per query and follows its changes', () => {
    const spy = vi.spyOn(window, 'matchMedia');
    const { result, rerender } = renderHook(() => useMediaQuery('(max-width: 767px)'));
    rerender();
    rerender();
    expect(result.current).toBe(false);
    act(() => setMedia((q) => q === '(max-width: 767px)'));
    expect(result.current).toBe(true);
    expect(spy).toHaveBeenCalledTimes(1);
    spy.mockRestore();
  });
});
