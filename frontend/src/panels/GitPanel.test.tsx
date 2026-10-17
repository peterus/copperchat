import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiGet = vi.fn();
const send = vi.fn();
const chat = { runActive: false, send };
vi.mock('@/api/http', () => ({ apiGet: (...a: unknown[]) => apiGet(...a) }));
vi.mock('@/hooks/useChat', () => ({ useChat: () => chat }));

import { GitPanel, commitRequestText } from './GitPanel';

describe('GitPanel', () => {
  beforeEach(() => {
    chat.runActive = false;
    send.mockReset().mockReturnValue(true);
    apiGet.mockReset().mockResolvedValue({
      isRepo: true,
      branch: 'main',
      clean: false,
      files: [{ path: 'boards/demo/demo.kicad_pcb', status: 'M' }],
    });
  });

  it('commit text always contains the word commit (backend guard)', () => {
    expect(commitRequestText('rev B')).toBe('Please commit the current changes. Suggested message: rev B');
    expect(commitRequestText('  ')).toBe('Please commit the current changes.');
    expect(/\bcommit\b/i.test(commitRequestText('x'))).toBe(true);
  });

  it('shows branch and changed files', async () => {
    render(<GitPanel projectId="p1" />);
    expect(await screen.findByText('boards/demo/demo.kicad_pcb')).toBeInTheDocument();
    expect(screen.getByText('main')).toBeInTheDocument();
    expect(apiGet).toHaveBeenCalledWith('/projects/p1/git/status');
  });

  it('Commit sends a chat message instead of committing directly', async () => {
    render(<GitPanel projectId="p1" />);
    await screen.findByText('boards/demo/demo.kicad_pcb');
    const input = screen.getByLabelText(/suggested message/i) as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'rev B' } });
    fireEvent.click(screen.getByRole('button', { name: /^commit/i }));
    expect(send).toHaveBeenCalledWith('Please commit the current changes. Suggested message: rev B');
    expect(input.value).toBe('');
  });

  it('keeps the draft when the chat refused the message', async () => {
    send.mockReturnValue(false);
    render(<GitPanel projectId="p1" />);
    await screen.findByText('boards/demo/demo.kicad_pcb');
    const input = screen.getByLabelText(/suggested message/i) as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'rev B' } });
    fireEvent.click(screen.getByRole('button', { name: /^commit/i }));
    expect(input.value).toBe('rev B');
  });

  it('disables Commit when clean or while the agent runs, and explains when not a repo', async () => {
    apiGet.mockResolvedValueOnce({ isRepo: true, branch: 'main', clean: true, files: [] });
    const first = render(<GitPanel projectId="p1" />);
    await waitFor(() => expect(screen.getByRole('button', { name: /^commit/i })).toBeDisabled());
    first.unmount();
    apiGet.mockResolvedValueOnce({ isRepo: false, branch: null, clean: true, files: [] });
    const second = render(<GitPanel projectId="p1" />);
    expect(await screen.findByText(/not in a git repository/i)).toBeInTheDocument();
    second.unmount();
    chat.runActive = true;
    render(<GitPanel projectId="p1" />);
    expect(await screen.findByRole('button', { name: /^commit/i })).toBeDisabled();
  });

  it('shows a load error', async () => {
    apiGet.mockRejectedValue(new Error('HTTP 500'));
    render(<GitPanel projectId="p1" />);
    expect(await screen.findByRole('alert')).toHaveTextContent('HTTP 500');
  });
  it('shows how many files were cut off and a git refusal', async () => {
    apiGet.mockResolvedValueOnce({
      isRepo: true, branch: 'main', clean: false, total: 250, truncated: true,
      files: [{ path: 'a.txt', status: '??' }],
    });
    const first = render(<GitPanel projectId="p1" />);
    expect(await screen.findByText('… and 249 more')).toBeInTheDocument();
    first.unmount();
    apiGet.mockResolvedValueOnce({
      isRepo: false, branch: null, clean: true, files: [], error: 'git refuses the repository',
    });
    render(<GitPanel projectId="p1" />);
    expect(await screen.findByRole('alert')).toHaveTextContent('git refuses the repository');
  });
});
