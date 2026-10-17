import { create } from 'zustand';

/**
 * Size limits (MB of KiCad source per document) above which the interactive KiCanvas view is not
 * loaded automatically; the agent-view PNG is shown instead, with an explicit "load anyway".
 * Per device (localStorage): what a phone can parse depends on the phone. An 85 MB board crashed
 * an iPhone Safari tab; parsing it took ~130 s on a desktop CPU.
 */
export interface ViewerLimits {
  /** Touch devices (phones, tablets). */
  mobileMb: number;
  desktopMb: number;
}

export const DEFAULT_VIEWER_LIMITS: ViewerLimits = { mobileMb: 15, desktopMb: 60 };
export const VIEWER_LIMITS_KEY = 'copperchat.viewer.limits';

const valid = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n) && n > 0;

function read(): ViewerLimits {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(VIEWER_LIMITS_KEY) ?? 'null');
    if (raw && typeof raw === 'object') {
      const r = raw as Partial<Record<keyof ViewerLimits, unknown>>;
      return {
        mobileMb: valid(r.mobileMb) ? r.mobileMb : DEFAULT_VIEWER_LIMITS.mobileMb,
        desktopMb: valid(r.desktopMb) ? r.desktopMb : DEFAULT_VIEWER_LIMITS.desktopMb,
      };
    }
  } catch {
    // unreadable / unavailable storage: defaults
  }
  return { ...DEFAULT_VIEWER_LIMITS };
}

interface ViewerPrefsState {
  limits: ViewerLimits;
  setLimit: (key: keyof ViewerLimits, mb: number) => void;
}

export const useViewerPrefs = create<ViewerPrefsState>()((set, get) => ({
  limits: read(),
  setLimit: (key, mb) => {
    if (!valid(mb)) return;
    const limits = { ...get().limits, [key]: mb };
    try {
      localStorage.setItem(VIEWER_LIMITS_KEY, JSON.stringify(limits));
    } catch {
      // storage unavailable (private mode): applies for this session only
    }
    set({ limits });
  },
}));

export interface LargeFileGuard {
  bytes: number;
  limitMb: number;
  mobile: boolean;
}

/** The guard for a document of `bytes`, or null when it may load interactively. */
export function guardFor(bytes: number, mobile: boolean, limits: ViewerLimits): LargeFileGuard | null {
  const limitMb = mobile ? limits.mobileMb : limits.desktopMb;
  return bytes > limitMb * 1_000_000 ? { bytes, limitMb, mobile } : null;
}

export function formatBytes(n: number): string {
  return n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1000))} kB`;
}
