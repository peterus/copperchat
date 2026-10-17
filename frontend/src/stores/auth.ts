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
  if (e instanceof ApiError && e.status >= 500) {
    return `The server could not sign you in (HTTP ${e.status}). Try again in a moment.`;
  }
  // fetch() rejects with a TypeError when the server is unreachable.
  if (e instanceof TypeError) return 'Cannot reach the server. Check your connection and try again.';
  return messageOf(e);
}

/** The login succeeded (204) but the next request carried no session cookie. */
export function cookieNotStoredError(secureContext: boolean): string {
  // Browsers drop Secure cookies outside a secure context (plain http, except localhost).
  return secureContext
    ? 'Signed in, but your browser did not keep the session cookie. Allow cookies for this site and try again.'
    : 'Signed in, but your browser did not keep the session cookie: it is only sent over HTTPS. ' +
        'Use HTTPS (see docs/deployment/cloudflare.md) or set COOKIE_SECURE=false for plain-http LAN access. ' +
        'If COOKIE_SECURE is already false, allow cookies for this site.';
}

class CookieNotStored extends Error {}

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
      // Confirm the browser kept the cookie: a dropped Secure cookie over plain http would
      // otherwise bounce the user back to /login without a word (first 401 after the redirect).
      const me = await apiGet<unknown>('/auth/me').catch((e: unknown) => {
        throw e instanceof ApiError && e.status === 401 ? new CookieNotStored() : e;
      });
      if (get().status === 'authenticated') {
        // Re-login over a live session (possibly another user): drop the old socket and pass
        // through 'anonymous' so installSessionReset() clears the previous session's state.
        wsClient.close();
        set({ status: 'anonymous', username: null });
      }
      const parsed = Me.safeParse(me);
      set({ status: 'authenticated', username: parsed.success ? parsed.data.username : username, error: null });
      return true;
    } catch (e) {
      const error = e instanceof CookieNotStored ? cookieNotStoredError(window.isSecureContext) : loginError(e);
      set({ status: 'anonymous', username: null, error });
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
