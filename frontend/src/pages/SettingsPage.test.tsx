import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { useAuth } from '@/stores/auth';
import { useUi } from '@/stores/ui';
import { SettingsPage } from './SettingsPage';

describe('SettingsPage', () => {
  it('switches theme, signs out, and is branded correctly', async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', fetchMock);
    useAuth.setState({ status: 'authenticated', username: 'admin', error: null });
    render(
      <MemoryRouter initialEntries={['/settings']}>
        <Routes>
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/login" element={<p>login page</p>} />
        </Routes>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole('radio', { name: 'Dark' }));
    expect(useUi.getState().theme).toBe('dark');
    expect(screen.getByRole('radio', { name: 'Dark' })).toBeChecked();
    expect(screen.getByText('Signed in as admin')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Notifications' })).toBeInTheDocument();
    expect(screen.getByText(/Powered by Claude/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/Claude Code/);
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    await waitFor(() => expect(screen.getByText('login page')).toBeInTheDocument());
    expect(useAuth.getState().status).toBe('anonymous');
    expect(fetchMock).toHaveBeenCalledWith('/api/auth/logout', expect.objectContaining({ method: 'POST' }));
  });
});
