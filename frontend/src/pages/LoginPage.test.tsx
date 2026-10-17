import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useAuth } from '@/stores/auth';
import { LoginPage } from './LoginPage';
import { RequireAuth } from './RequireAuth';

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<RequireAuth />}>
          <Route path="/projects" element={<p>projects page</p>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json', ...headers } });

async function signIn(password = 'secret') {
  await userEvent.type(await screen.findByLabelText('Username'), 'admin');
  await userEvent.type(screen.getByLabelText('Password'), password);
  await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
}

describe('login', () => {
  beforeEach(() => useAuth.setState({ status: 'unknown', username: null, error: null }));
  afterEach(() => vi.unstubAllGlobals());

  it('redirects anonymous users to /login and signs in', async () => {
    let authed = false;
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/auth/me') return authed ? json(200, { username: 'admin' }) : json(401, { detail: 'no' });
      if (url === '/api/auth/login') {
        authed = true;
        return new Response(null, { status: 204 });
      }
      return new Response(null, { status: 404 });
    }));
    renderAt('/projects');
    await userEvent.type(await screen.findByLabelText('Username'), 'admin');
    await userEvent.type(screen.getByLabelText('Password'), 'secret');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByText('projects page')).toBeInTheDocument();
    expect(useAuth.getState().username).toBe('admin');
  });

  it('shows an error on wrong credentials', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json(401, { detail: 'bad' })));
    renderAt('/login');
    await userEvent.type(screen.getByLabelText('Username'), 'admin');
    await userEvent.type(screen.getByLabelText('Password'), 'nope');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Wrong username or password.');
    expect(screen.getByText(/Powered by Claude/)).toBeInTheDocument();
    expect(screen.queryByText(/Claude Code/)).not.toBeInTheDocument();
  });

  it('tells the user how long to wait after too many failed sign-ins (429)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json(429, { detail: 'too many failed logins' }, { 'Retry-After': '30' })));
    renderAt('/login');
    await userEvent.type(screen.getByLabelText('Username'), 'admin');
    await userEvent.type(screen.getByLabelText('Password'), 'nope');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Too many failed sign-ins. Try again in 30 s.');
  });

  it('redirects an already signed-in user away from /login', () => {
    useAuth.setState({ status: 'authenticated', username: 'admin', error: null });
    render(
      <MemoryRouter initialEntries={['/login']}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/" element={<p>home</p>} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText('home')).toBeInTheDocument();
    expect(screen.queryByLabelText('Username')).toBeNull();
  });

  it('keeps the form filled in after wrong credentials', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json(401, { detail: 'bad' })));
    renderAt('/login');
    await signIn('nope');
    expect(await screen.findByRole('alert')).toHaveTextContent('Wrong username or password.');
    expect(screen.getByLabelText('Username')).toHaveValue('admin');
    expect(screen.getByLabelText('Password')).toHaveValue('nope');
    expect(screen.getByRole('alert').parentElement).toHaveAttribute('aria-live', 'assertive');
  });

  it('reports a server error (5xx)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('Bad Gateway', { status: 502 })));
    renderAt('/login');
    await signIn();
    expect(await screen.findByRole('alert')).toHaveTextContent('The server could not sign you in (HTTP 502).');
  });

  it('reports an unreachable server', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(new TypeError('Failed to fetch'))));
    renderAt('/login');
    await signIn();
    expect(await screen.findByRole('alert')).toHaveTextContent('Cannot reach the server.');
  });

  it.each([
    [false, /only sent over HTTPS.*COOKIE_SECURE=false/],
    [true, /Allow cookies for this site/],
  ])('explains a session cookie the browser dropped (secure context: %s)', async (secure, text) => {
    vi.stubGlobal('isSecureContext', secure);
    // 204 on login, but the cookie never comes back: /auth/me stays 401.
    const fetchMock = vi.fn(async (url: string) =>
      url === '/api/auth/login' ? new Response(null, { status: 204 }) : json(401, { detail: 'not logged in' }),
    );
    vi.stubGlobal('fetch', fetchMock);
    renderAt('/projects');
    await signIn();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Signed in, but your browser did not keep the session cookie');
    expect(alert).toHaveTextContent(text);
    expect(screen.queryByText('projects page')).toBeNull();
    expect(screen.getByLabelText('Username')).toHaveValue('admin');
    expect(useAuth.getState().status).toBe('anonymous');
    // one login, one check — no retry loop
    expect(fetchMock.mock.calls.filter(([u]) => u === '/api/auth/login')).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeEnabled();
  });
});
