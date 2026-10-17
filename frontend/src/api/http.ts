export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;
  /** Seconds from a `Retry-After` header (e.g. 429 after too many failed logins), else null. */
  readonly retryAfterS: number | null;
  constructor(status: number, detail: unknown, message: string, retryAfterS: number | null = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.retryAfterS = retryAfterS;
  }
}

let unauthorizedHandler: (() => void) | null = null;

/** The auth store registers itself here; avoids an import cycle http ↔ auth. */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

/** Callers pass paths without the /api prefix (index X10): '/projects' → '/api/projects'. */
export function apiUrl(path: string): string {
  return `/api${path.startsWith('/') ? path : `/${path}`}`;
}

async function readBody(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) return null;
  if ((res.headers.get('content-type') ?? '').includes('application/json')) {
    try {
      return JSON.parse(text) as unknown;
    } catch {
      return text;
    }
  }
  return text;
}

function errorMessage(status: number, body: unknown): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === 'string') return detail;
  }
  // CopperChatError responses (backend install_error_handlers): {error: {code, message}}.
  if (body && typeof body === 'object' && 'error' in body) {
    const error = (body as { error: unknown }).error;
    if (error && typeof error === 'object' && typeof (error as { message?: unknown }).message === 'string') {
      return (error as { message: string }).message;
    }
  }
  if (typeof body === 'string' && body.length > 0) return body;
  return `HTTP ${status}`;
}

function retryAfter(res: Response): number | null {
  const raw = res.headers.get('Retry-After');
  if (raw === null) return null;
  const seconds = Number(raw);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  const init: RequestInit = { method, credentials: 'same-origin', headers };
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const res = await fetch(apiUrl(path), init);
  if (res.status === 401 && !path.startsWith('/auth/login')) unauthorizedHandler?.();
  if (!res.ok) {
    const parsed = await readBody(res);
    throw new ApiError(res.status, parsed, errorMessage(res.status, parsed), retryAfter(res));
  }
  if (res.status === 204) return undefined as T;
  return (await readBody(res)) as T;
}

export const apiGet = <T>(path: string): Promise<T> => request<T>('GET', path);
export const apiPost = <T>(path: string, body: unknown): Promise<T> => request<T>('POST', path, body);
export const apiPut = <T>(path: string, body: unknown): Promise<T> => request<T>('PUT', path, body);
export const apiDelete = (path: string): Promise<void> => request<void>('DELETE', path);
