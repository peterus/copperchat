import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import * as push from '@/push/push';
import { NotificationsSection } from './NotificationsSection';

beforeEach(() => {
  vi.restoreAllMocks();
});

it('shows the iOS install hint when not standalone', async () => {
  vi.spyOn(push, 'pushSupport').mockReturnValue('unsupported');
  vi.spyOn(push, 'isIos').mockReturnValue(true);
  vi.spyOn(push, 'isStandalone').mockReturnValue(false);
  render(<NotificationsSection />);
  expect(await screen.findByText(/Add to Home Screen/)).toBeInTheDocument();
  expect(screen.getByText(/16\.4/)).toBeInTheDocument();
});

it('explains HTTPS when push is unsupported elsewhere', async () => {
  vi.spyOn(push, 'pushSupport').mockReturnValue('unsupported');
  vi.spyOn(push, 'isIos').mockReturnValue(false);
  render(<NotificationsSection />);
  expect(await screen.findByText(/HTTPS/)).toBeInTheDocument();
});

it('subscribes and unsubscribes', async () => {
  vi.spyOn(push, 'pushSupport').mockReturnValue('default');
  vi.spyOn(push, 'isIos').mockReturnValue(false);
  vi.spyOn(push, 'currentSubscription').mockResolvedValue(null);
  const sub = vi.spyOn(push, 'subscribePush').mockResolvedValue();
  const unsub = vi.spyOn(push, 'unsubscribePush').mockResolvedValue();
  render(<NotificationsSection />);
  await userEvent.click(await screen.findByRole('button', { name: 'Enable notifications' }));
  expect(sub).toHaveBeenCalled();
  await userEvent.click(await screen.findByRole('button', { name: 'Disable notifications' }));
  expect(unsub).toHaveBeenCalled();
  expect(await screen.findByRole('button', { name: 'Enable notifications' })).toBeEnabled();
});

it('shows an existing subscription and errors', async () => {
  vi.spyOn(push, 'pushSupport').mockReturnValue('granted');
  vi.spyOn(push, 'isIos').mockReturnValue(false);
  vi.spyOn(push, 'currentSubscription').mockResolvedValue({} as PushSubscription);
  vi.spyOn(push, 'unsubscribePush').mockRejectedValue(new Error('network down'));
  render(<NotificationsSection />);
  await userEvent.click(await screen.findByRole('button', { name: 'Disable notifications' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('network down');
});

it('shows the blocked state', async () => {
  vi.spyOn(push, 'pushSupport').mockReturnValue('denied');
  vi.spyOn(push, 'isIos').mockReturnValue(false);
  vi.spyOn(push, 'currentSubscription').mockResolvedValue(null);
  render(<NotificationsSection />);
  expect(await screen.findByText(/blocked in your browser settings/)).toBeInTheDocument();
});
