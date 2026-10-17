import { beforeEach, describe, expect, it } from 'vitest';
import { DEFAULT_VIEWER_LIMITS, formatBytes, guardFor, useViewerPrefs, VIEWER_LIMITS_KEY } from './viewerPrefs';

beforeEach(() => {
  localStorage.clear();
  useViewerPrefs.setState({ limits: { ...DEFAULT_VIEWER_LIMITS } });
});

describe('guardFor', () => {
  const limits = { mobileMb: 15, desktopMb: 60 };
  it('guards an 85 MB board on phones and desktops', () => {
    expect(guardFor(85_500_000, true, limits)).toEqual({ bytes: 85_500_000, limitMb: 15, mobile: true });
    expect(guardFor(85_500_000, false, limits)).toEqual({ bytes: 85_500_000, limitMb: 60, mobile: false });
  });
  it('lets a 5.9 MB board load interactively everywhere', () => {
    expect(guardFor(5_900_000, true, limits)).toBeNull();
    expect(guardFor(5_900_000, false, limits)).toBeNull();
  });
  it('uses the per-device limit', () => {
    expect(guardFor(20_000_000, true, limits)).not.toBeNull();
    expect(guardFor(20_000_000, false, limits)).toBeNull();
  });
});

describe('viewer prefs', () => {
  it('persists valid limits and ignores invalid ones', () => {
    useViewerPrefs.getState().setLimit('mobileMb', 30);
    expect(useViewerPrefs.getState().limits.mobileMb).toBe(30);
    expect(JSON.parse(localStorage.getItem(VIEWER_LIMITS_KEY)!)).toEqual({ mobileMb: 30, desktopMb: 60 });
    useViewerPrefs.getState().setLimit('desktopMb', Number.NaN);
    useViewerPrefs.getState().setLimit('desktopMb', 0);
    expect(useViewerPrefs.getState().limits.desktopMb).toBe(60);
  });
  it('formats sizes', () => {
    expect(formatBytes(85_500_000)).toBe('85.5 MB');
    expect(formatBytes(161_000)).toBe('161 kB');
  });
});
