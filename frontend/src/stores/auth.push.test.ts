import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import * as http from '@/api/http';
import { wsClient } from '@/api/ws';
import * as push from '@/push/push';
import { useAuth } from './auth';

beforeEach(() => {
  vi.spyOn(push, 'forgetPushSubscription').mockResolvedValue();
  vi.spyOn(push, 'unsubscribePush').mockResolvedValue();
});
afterEach(() => wsClient.close());

it('a network error at boot keeps the push subscription', async () => {
  useAuth.setState({ status: 'unknown', username: null, error: null });
  vi.spyOn(http, 'apiGet').mockRejectedValue(new TypeError('Failed to fetch'));
  await useAuth.getState().check();
  expect(useAuth.getState().status).toBe('anonymous');
  expect(push.forgetPushSubscription).not.toHaveBeenCalled();
});

it('a 401 at boot forgets the browser subscription', async () => {
  useAuth.setState({ status: 'unknown', username: null, error: null });
  vi.spyOn(http, 'apiGet').mockRejectedValue(new http.ApiError(401, null, 'not authenticated'));
  await useAuth.getState().check();
  expect(push.forgetPushSubscription).toHaveBeenCalled();
});

it('logout removes the subscription on the server before the cookie goes', async () => {
  useAuth.setState({ status: 'authenticated', username: 'admin', error: null });
  const order: string[] = [];
  vi.mocked(push.unsubscribePush).mockImplementation(async () => void order.push('unsubscribe'));
  vi.spyOn(http, 'apiPost').mockImplementation(async () => void order.push('logout'));
  await useAuth.getState().logout();
  expect(order).toEqual(['unsubscribe', 'logout']);
  expect(push.forgetPushSubscription).not.toHaveBeenCalled();
});

it('logout still forgets the browser subscription when the server DELETE fails', async () => {
  useAuth.setState({ status: 'authenticated', username: 'admin', error: null });
  vi.mocked(push.unsubscribePush).mockRejectedValue(new Error('offline'));
  vi.spyOn(http, 'apiPost').mockResolvedValue(undefined);
  await useAuth.getState().logout();
  expect(push.forgetPushSubscription).toHaveBeenCalled();
  expect(useAuth.getState().status).toBe('anonymous');
});

it('a server-side sign-out (401 / 4401) forgets the subscription', () => {
  useAuth.setState({ status: 'authenticated', username: 'admin', error: null });
  useAuth.getState().handleUnauthorized();
  expect(push.forgetPushSubscription).toHaveBeenCalled();
});
