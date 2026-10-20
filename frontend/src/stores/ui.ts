import { create } from 'zustand';

export type ThemePref = 'light' | 'dark' | 'system';
export type ResolvedTheme = 'light' | 'dark';
/** Also read by the inline script in index.html (no flash of the wrong theme). */
export const THEME_KEY = 'copperchat.theme';
const DARK_QUERY = '(prefers-color-scheme: dark)';

export function resolveTheme(pref: ThemePref, prefersDark: boolean): ResolvedTheme {
  if (pref === 'system') return prefersDark ? 'dark' : 'light';
  return pref;
}

function readPref(): ThemePref {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return v === 'light' || v === 'dark' || v === 'system' ? v : 'system';
  } catch {
    return 'system';
  }
}

const prefersDark = () => typeof window.matchMedia === 'function' && window.matchMedia(DARK_QUERY).matches;

function apply(resolved: ResolvedTheme): void {
  document.documentElement.dataset.theme = resolved;
}

/** A request to show a side panel (S-C8); ProjectLayout consumes it (clearPanelRequest). */
export interface PanelRequest {
  id: string;
  params?: Record<string, string>;
}

interface UiState {
  /** The pending openPanel() request, or null once ProjectLayout has opened it. */
  panelRequest: PanelRequest | null;
  /** Opens the registered panel `id` (desktop: sheet, phone: its tab), passing `params` as `PanelProps.params`. */
  openPanel: (id: string, params?: Record<string, string>) => void;
  clearPanelRequest: () => void;
  theme: ThemePref;
  /** What is actually shown; 6B passes it as the agent-view `theme=` parameter. */
  resolvedTheme: ResolvedTheme;
  setTheme: (t: ThemePref) => void;
  /** Re-resolve after a prefers-color-scheme change. */
  syncSystem: () => void;
}

export const useUi = create<UiState>()((set, get) => ({
  panelRequest: null,
  openPanel: (id, params) => set({ panelRequest: params ? { id, params } : { id } }),
  clearPanelRequest: () => set({ panelRequest: null }),
  theme: readPref(),
  resolvedTheme: resolveTheme(readPref(), prefersDark()),
  setTheme: (theme) => {
    try {
      localStorage.setItem(THEME_KEY, theme);
    } catch {
      // storage unavailable (private mode): the choice still applies for this session
    }
    const resolvedTheme = resolveTheme(theme, prefersDark());
    apply(resolvedTheme);
    set({ theme, resolvedTheme });
  },
  syncSystem: () => {
    const resolvedTheme = resolveTheme(get().theme, prefersDark());
    apply(resolvedTheme);
    if (resolvedTheme !== get().resolvedTheme) set({ resolvedTheme });
  },
}));

/** Applies data-theme now and follows prefers-color-scheme; returns the unsubscribe. */
export function initTheme(): () => void {
  useUi.getState().syncSystem();
  const mql = window.matchMedia(DARK_QUERY);
  const onChange = () => useUi.getState().syncSystem();
  mql.addEventListener('change', onChange);
  return () => mql.removeEventListener('change', onChange);
}
