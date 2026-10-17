import { describe, expect, it, vi } from 'vitest';
import { ApiError, apiDelete, apiGet, apiPost, apiPut, apiUrl, setUnauthorizedHandler } from './http';

function respond(status: number, body?: unknown, headers: Record<string, string> = {}) {
  const init = { status, headers: { 'content-type': 'application/json', ...headers } };
  return new Response(body === undefined ? null : JSON.stringify(body), init);
}

describe('http client', () => {
  it('apiUrl adds the /api prefix', () => {
    expect(apiUrl('/projects')).toBe('/api/projects');
    expect(apiUrl('auth/me')).toBe('/api/auth/me');
  });

  it('prefixes /api, sends JSON and parses JSON', async () => {
    const fetchMock = vi.fn(async () => respond(200, { ok: true }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(apiPost<{ ok: boolean }>('/projects/x/open', { a: 1 })).resolves.toEqual({ ok: true });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/projects/x/open');
    expect(init.method).toBe('POST');
    expect(init.body).toBe('{"a":1}');
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json');
    expect(init.credentials).toBe('same-origin');
  });

  it('returns undefined for 204 and supports PUT/DELETE', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 204 })));
    await expect(apiPut('/x', {})).resolves.toBeUndefined();
    await expect(apiDelete('/x')).resolves.toBeUndefined();
  });

  it('throws ApiError with FastAPI detail text', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => respond(400, { detail: 'invalid project id' })));
    const err = await apiGet('/projects/..').catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(400);
    expect((err as ApiError).message).toBe('invalid project id');
    expect((err as ApiError).retryAfterS).toBeNull();
  });

  it('exposes Retry-After seconds on 429', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => respond(429, { detail: 'too many failed logins' }, { 'Retry-After': '42' })));
    const err = await apiPost('/auth/login', { username: 'a', password: 'b' }).catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(429);
    expect((err as ApiError).retryAfterS).toBe(42);
  });

  it('calls the unauthorized handler on 401, except for the login call itself', async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    vi.stubGlobal('fetch', vi.fn(async () => respond(401, { detail: 'not authenticated' })));
    await expect(apiGet('/projects')).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledTimes(1);
    await expect(apiPost('/auth/login', { username: 'a', password: 'b' })).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledTimes(1);
    setUnauthorizedHandler(null);
  });
});
