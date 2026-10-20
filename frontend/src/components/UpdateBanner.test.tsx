import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { setUnauthorizedHandler } from '@/api/http';
import { BUILD_VERSION } from '@/lib/buildVersion';
import { FakeWsClient } from '@/test/fakeWsClient';
import { UpdateBanner } from './UpdateBanner';

let ws: FakeWsClient;
let serverVersion: string;
let healthCalls: number;

beforeEach(() => {
  ws = new FakeWsClient();
  serverVersion = BUILD_VERSION;
  healthCalls = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url !== '/api/health') return new Response(null, { status: 404 });
      healthCalls += 1;
      return new Response(JSON.stringify({ status: 'ok', version: serverVersion, kicad: 'ok' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }),
  );
});

describe('UpdateBanner', () => {
  it('the build version comes from package.json via Vite define', () => {
    expect(BUILD_VERSION).toMatch(/^\d+\.\d+\.\d+/);
  });

  it('shows "CopperChat was updated — reload" when /api/health reports another version', async () => {
    serverVersion = '99.0.0';
    const reload = vi.fn();
    render(<UpdateBanner ws={ws} reload={reload} />);
    expect(screen.queryByRole('alert')).toBeNull();
    act(() => ws.connect());
    expect(await screen.findByRole('alert')).toHaveTextContent('CopperChat was updated — reload');
    fireEvent.click(screen.getByRole('button', { name: 'Reload' }));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('shows nothing when the versions match, and checks again on every reconnect', async () => {
    render(<UpdateBanner ws={ws} reload={vi.fn()} />);
    act(() => ws.connect());
    await waitFor(() => expect(healthCalls).toBe(1));
    expect(screen.queryByRole('alert')).toBeNull();
    serverVersion = '99.0.0';
    act(() => {
      ws.drop();
      ws.connect();
    });
    await waitFor(() => expect(healthCalls).toBe(2));
    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  it('a 401 from /api/health (e.g. a foreign server on the port) shows nothing and does not log out', async () => {
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 401, headers: { 'content-type': 'application/json' } })));
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    render(<UpdateBanner ws={ws} reload={vi.fn()} />);
    act(() => ws.connect());
    await waitFor(() => expect(warn).toHaveBeenCalled());
    expect(onUnauthorized).not.toHaveBeenCalled();
    expect(screen.queryByRole('alert')).toBeNull();
    setUnauthorizedHandler(null);
  });

  it('a failed health check shows nothing', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('down', { status: 503 })));
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    render(<UpdateBanner ws={ws} reload={vi.fn()} />);
    act(() => ws.connect());
    await waitFor(() => expect(warn).toHaveBeenCalled());
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
