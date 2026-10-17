import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
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

describe('login', () => {
  beforeEach(() => useAuth.setState({ status: 'unknown', username: null, error: null }));

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
});
