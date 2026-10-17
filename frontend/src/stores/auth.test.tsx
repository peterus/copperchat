import { act, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { wsClient } from '@/api/ws';
import { RequireAuth } from '@/pages/RequireAuth';
import { FakeWebSocket } from '@/test/fakeWebSocket';
import { useAuth } from './auth';

describe('auth store ↔ WebSocket 4401', () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal('WebSocket', FakeWebSocket);
    useAuth.setState({ status: 'authenticated', username: 'admin', error: null });
  });
  afterEach(() => wsClient.close());

  it('a 4401 close mid-connection logs out locally and routes to /login', () => {
    render(
      <MemoryRouter initialEntries={['/projects']}>
        <Routes>
          <Route path="/login" element={<p>login page</p>} />
          <Route element={<RequireAuth />}>
            <Route path="/projects" element={<p>projects page</p>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText('projects page')).toBeInTheDocument();
    wsClient.ensure();
    const ws = FakeWebSocket.latest();
    act(() => {
      ws.serverOpen();
      ws.serverClose(4401);
    });
    expect(useAuth.getState()).toMatchObject({ status: 'anonymous', username: null });
    expect(screen.getByText('login page')).toBeInTheDocument();
    // ensure() after a new login connects again.
    wsClient.ensure();
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it('a 4403 close does not log out', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    wsClient.ensure();
    const ws = FakeWebSocket.latest();
    ws.serverOpen();
    ws.serverClose(4403);
    expect(useAuth.getState().status).toBe('authenticated');
  });
});
