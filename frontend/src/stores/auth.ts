import { z } from 'zod';
import { create } from 'zustand';
import { ApiError, apiGet, apiPost, setUnauthorizedHandler } from '@/api/http';
import { wsClient } from '@/api/ws';
import { forgetPushSubscription, unsubscribePush } from '@/push/push';

const Me = z.object({ username: z.string() });

export type AuthStatus = 'unknown' | 'authenticated' | 'anonymous';

interface AuthState {
  status: AuthStatus;
  username: string | null;
  error: string | null;
  check: () => Promise<void>;
  login: (username: string, password: string) => Promise<boolean>;
  logout: () => Promise<void>;
  handleUnauthorized: () => void;
}

const messageOf = (e: unknown) => (e instanceof Error ? e.message : String(e));

function loginError(e: unknown): string {
  if (e instanceof ApiError && e.status === 401) return 'Wrong username or password.';
  if (e instanceof ApiError && e.status === 429) {
    return e.retryAfterS !== null
      ? `Too many failed sign-ins. Try again in ${e.retryAfterS} s.`
      : 'Too many failed sign-ins. Try again later.';
  }
  return messageOf(e);
}

export const useAuth = create<AuthState>()((set, get) => ({
  status: 'unknown',
  username: null,
  error: null,
  check: async () => {
    try {
      const me = Me.parse(await apiGet<unknown>('/auth/me'));
      set({ status: 'authenticated', username: me.username, error: null });
    } catch (e) {
      const signedOut = e instanceof ApiError && e.status === 401;
      // Only a confirmed sign-out turns push off; a network error at boot must not.
      if (signedOut) void forgetPushSubscription();
      set({ status: 'anonymous', username: null, error: signedOut ? null : messageOf(e) });
    }
  },
  login: async (username, password) => {
    try {
      await apiPost<void>('/auth/login', { username, password });
      if (get().status === 'authenticated') {
        // Re-login over a live session (possibly another user): drop the old socket and pass
        // through 'anonymous' so installSessionReset() clears the previous session's state.
        wsClient.close();
        set({ status: 'anonymous', username: null });
      }
      set({ status: 'authenticated', username, error: null });
      return true;
    } catch (e) {
      set({ status: 'anonymous', error: loginError(e) });
      return false;
    }
  },
  logout: async () => {
    // A signed-out device gets no notifications (they name projects and quote the agent).
    // While the cookie is still valid the server row goes too; otherwise only the browser's.
    await unsubscribePush().catch(() => forgetPushSubscription());
    try {
      await apiPost<void>('/auth/logout', {});
    } catch {
      // The cookie may already be invalid; local logout proceeds regardless.
    }
    wsClient.close();
    set({ status: 'anonymous', username: null, error: null });
  },
  handleUnauthorized: () => {
    void forgetPushSubscription(); // the server said: signed out (HTTP 401 / WebSocket 4401)
    wsClient.close();
    set({ status: 'anonymous', username: null });
  },
}));

setUnauthorizedHandler(() => useAuth.getState().handleUnauthorized());
// The WebSocket is closed with 4401 when the session is gone (e.g. logout in another tab):
// log out locally so RequireAuth routes to /login. Connecting resumes via wsClient.ensure().
wsClient.onConnection((state) => {
  if (state === 'unauthorized') useAuth.getState().handleUnauthorized();
});
