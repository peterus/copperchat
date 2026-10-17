import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AppRoutes } from './App';
import { useAuth } from './stores/auth';

beforeEach(() => useAuth.setState({ status: 'unknown', username: null, error: null }));

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

describe('App routes', () => {
  it('unknown paths go to /projects which requires login', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ detail: 'no' }, 401)));
    render(
      <MemoryRouter initialEntries={['/nowhere']}>
        <AppRoutes />
      </MemoryRouter>,
    );
    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument();
    expect(screen.getByText(/Powered by Claude/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/Claude Code/);
  });

  it('authenticated users see the project picker', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => json(url === '/api/auth/me' ? { username: 'admin' } : [])));
    render(
      <MemoryRouter initialEntries={['/projects']}>
        <AppRoutes />
      </MemoryRouter>,
    );
    expect(await screen.findByRole('heading', { name: 'Projects' })).toBeInTheDocument();
  });

  it('/settings is behind login too', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => json(url === '/api/auth/me' ? { username: 'admin' } : [])));
    render(
      <MemoryRouter initialEntries={['/settings']}>
        <AppRoutes />
      </MemoryRouter>,
    );
    expect(await screen.findByText('Signed in as admin')).toBeInTheDocument();
  });
});
