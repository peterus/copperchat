import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PHONE_QUERY } from '@/layouts/ProjectLayout';
import { conv } from '@/test/conv';
import { PID, row } from '@/test/fakeMessageServer';
import { resetMedia, setMedia } from '@/test/media';
import { fakeConversations, renderWithSessions } from '@/test/sessions';
import { SessionList } from './SessionList';

const C1 = '11111111111111111111111111111111';
const C2 = '22222222222222222222222222222222';
const C3 = '33333333333333333333333333333333';
const C4 = '44444444444444444444444444444444';
let f: ReturnType<typeof fakeConversations>;
let onClose: ReturnType<typeof vi.fn<() => void>>;

beforeEach(async () => {
  f = fakeConversations([
    conv(C1, 1, { lastMessageAt: '2026-10-19T12:00:00.000Z', usage: { inputTokens: 1, outputTokens: 1, costUsd: 0.1234 } }),
    conv(C2, 2, { type: 'requirements', status: 'closed', closedAt: '2026-10-19T11:00:00.000Z' }),
    conv(C3, 3, { type: 'schematic', lastMessageAt: '2026-10-19T13:00:00.000Z' }),
    conv(C4, 4, { archived: true, title: 'Old idea' }),
  ]);
  await f.store.getState().load(PID);
  f.store.getState().view(PID, C1);
  onClose = vi.fn<() => void>();
});
afterEach(() => resetMedia());

const list = () => screen.getByRole('list', { name: 'Sessions' });
const rowOf = (title: string) => within(list()).getByText(title).closest('li')!;

function renderList() {
  return renderWithSessions(<SessionList projectId={PID} currentId={C1} onClose={onClose} />, f.store, `/p/${PID}/s/${C1}`);
}

describe('SessionList', () => {
  it('lists newest activity first with type, cost; closed sessions dimmed; archived hidden', () => {
    renderList();
    const titles = within(list()).getAllByRole('link').map((a) => a.textContent);
    expect(titles.map((t) => t?.match(/Session \d/)?.[0])).toEqual(['Session 3', 'Session 1', 'Session 2']);
    expect(within(rowOf('Session 1')).getByText('$0.12')).toBeInTheDocument();
    expect(within(rowOf('Session 3')).getByRole('img', { name: 'Schematic session' })).toBeInTheDocument();
    expect(rowOf('Session 2')).toHaveAttribute('data-closed', 'true');
    expect(rowOf('Session 1')).not.toHaveAttribute('data-closed');
    expect(within(list()).queryByText('Old idea')).toBeNull();
    expect(within(rowOf('Session 1')).getByRole('link')).toHaveAttribute('aria-current', 'page');
  });

  it('shows running and unread dots', () => {
    renderList();
    act(() => {
      f.ws.emit({ type: 'run-started', projectId: PID, conversationId: C3, runId: 'r1' });
      f.ws.emit({ type: 'message', projectId: PID, message: row(9, { conversationId: C2 }) });
    });
    expect(within(rowOf('Session 3')).getByLabelText('running')).toBeInTheDocument();
    expect(within(rowOf('Session 2')).getByLabelText('unread')).toBeInTheDocument();
    expect(within(rowOf('Session 1')).queryByLabelText('running')).toBeNull();
    expect(within(rowOf('Session 1')).queryByLabelText('unread')).toBeNull();
  });

  it('picking a session navigates there and closes the list', () => {
    renderList();
    fireEvent.click(within(rowOf('Session 3')).getByRole('link'));
    expect(screen.getByTestId('location')).toHaveTextContent(`/p/${PID}/s/${C3}`);
    expect(onClose).toHaveBeenCalled();
  });

  it('archives, shows archived and unarchives', async () => {
    renderList();
    fireEvent.click(within(rowOf('Session 3')).getByRole('button', { name: 'Archive Session 3' }));
    await waitFor(() => expect(f.api.patch).toHaveBeenCalledWith(PID, C3, { archived: true }));
    await waitFor(() => expect(within(list()).queryByText('Session 3')).toBeNull());
    fireEvent.click(screen.getByRole('checkbox', { name: 'Show archived' }));
    expect(await within(list()).findByText('Old idea')).toBeInTheDocument();
    expect(f.api.list).toHaveBeenCalledWith(PID, { archived: true });
    fireEvent.click(within(rowOf('Old idea')).getByRole('button', { name: 'Unarchive Old idea' }));
    await waitFor(() => expect(f.api.patch).toHaveBeenCalledWith(PID, C4, { archived: false }));
  });

  it('renames a row inline', async () => {
    renderList();
    fireEvent.click(within(rowOf('Session 3')).getByRole('button', { name: 'Rename Session 3' }));
    const box = screen.getByRole('textbox', { name: 'Session title' });
    fireEvent.change(box, { target: { value: 'MCU' } });
    fireEvent.keyDown(box, { key: 'Enter' });
    await waitFor(() => expect(f.api.patch).toHaveBeenCalledWith(PID, C3, { title: 'MCU' }));
  });

  it('closes an open session from its row', async () => {
    renderList();
    expect(within(rowOf('Session 2')).queryByRole('button', { name: /^Close/ })).toBeNull();
    fireEvent.click(within(rowOf('Session 3')).getByRole('button', { name: 'Close Session 3' }));
    await waitFor(() => expect(f.api.close).toHaveBeenCalledWith(PID, C3));
  });

  it('is a popover on desktop and a full-height sheet on the phone', () => {
    const { unmount } = renderList();
    expect(screen.getByTestId('session-list')).toHaveAttribute('data-variant', 'popover');
    unmount();
    setMedia((q) => q === PHONE_QUERY);
    renderList();
    expect(screen.getByRole('dialog', { name: 'Sessions' })).toHaveAttribute('data-variant', 'sheet');
    fireEvent.click(screen.getByRole('button', { name: 'Close sessions' }));
    expect(onClose).toHaveBeenCalled();
  });

  it('Escape closes the list', () => {
    renderList();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalled();
  });
});
