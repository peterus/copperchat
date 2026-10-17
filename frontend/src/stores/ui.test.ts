import { describe, expect, it } from 'vitest';
import { setMedia } from '@/test/media';
import { THEME_KEY, initTheme, resolveTheme, useUi } from './ui';

describe('theme', () => {
  it('resolves system preference', () => {
    expect(resolveTheme('system', true)).toBe('dark');
    expect(resolveTheme('system', false)).toBe('light');
    expect(resolveTheme('light', true)).toBe('light');
    expect(resolveTheme('dark', false)).toBe('dark');
  });

  it('persists the preference and applies data-theme on :root, following the OS in system mode', () => {
    const stop = initTheme();
    useUi.getState().setTheme('dark');
    expect(localStorage.getItem(THEME_KEY)).toBe('dark');
    expect(document.documentElement.dataset.theme).toBe('dark');
    useUi.getState().setTheme('system');
    expect(document.documentElement.dataset.theme).toBe('light');
    setMedia((q) => q === '(prefers-color-scheme: dark)');
    expect(useUi.getState().resolvedTheme).toBe('dark');
    expect(document.documentElement.dataset.theme).toBe('dark');
    // an explicit choice ignores the OS
    useUi.getState().setTheme('light');
    setMedia(() => true);
    expect(document.documentElement.dataset.theme).toBe('light');
    stop();
  });
});
