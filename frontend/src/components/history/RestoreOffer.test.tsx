import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiPost = vi.fn();
vi.mock('@/api/http', () => ({ apiPost: (...a: unknown[]) => apiPost(...a) }));

import { MessageItem } from '@/components/chat/MessageItem';
import { CID, PID, row } from '@/test/fakeMessageServer';

const SNAP = 'c'.repeat(32);
const offer = row(5, {
  role: 'system',
  kind: 'notice',
  content: 'A snapshot from before this turn exists.',
  metadata: { action: 'restore', snapshotId: SNAP },
});

describe('restore offer on crash notices', () => {
  beforeEach(() => {
    apiPost.mockReset().mockResolvedValue({ id: SNAP, files: [] });
  });

  it('restores only after confirmation', async () => {
    render(<MessageItem message={offer} />);
    fireEvent.click(screen.getByRole('button', { name: /restore snapshot/i }));
    expect(apiPost).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /^restore$/i }));
    // the restore belongs to the conversation of the notice
    await waitFor(() =>
      expect(apiPost).toHaveBeenCalledWith(`/projects/${PID}/history/${SNAP}/restore`, { conversationId: CID }),
    );
    expect(await screen.findByText('Restored.')).toBeInTheDocument();
  });

  it('shows a refusal and keeps the offer', async () => {
    apiPost.mockRejectedValue(new Error('the agent is working; stop it or wait before restoring'));
    render(<MessageItem message={offer} />);
    fireEvent.click(screen.getByRole('button', { name: /restore snapshot/i }));
    fireEvent.click(screen.getByRole('button', { name: /^restore$/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent('agent is working');
    expect(screen.getByRole('button', { name: /restore snapshot/i })).toBeInTheDocument();
  });

  it('plain notices and other actions have no button', () => {
    render(<MessageItem message={row(6, { role: 'system', kind: 'notice', metadata: { action: 'restored', snapshotId: SNAP } })} />);
    expect(screen.queryByRole('button')).toBeNull();
  });
});
