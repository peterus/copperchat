import { fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiGet = vi.fn();
const apiPost = vi.fn();
const chat = { runActive: false };
vi.mock('@/api/http', () => ({
  apiGet: (...a: unknown[]) => apiGet(...a),
  apiPost: (...a: unknown[]) => apiPost(...a),
}));
vi.mock('@/hooks/useChat', () => ({ useChat: () => chat }));

import { usePanels } from '@/layouts/panels';
import { HistoryPanel } from './HistoryPanel';

const B = 'b'.repeat(32);
const A = 'a'.repeat(32);
const entries = [
  {
    id: B, runId: 'r2', createdAt: '2026-10-16T08:00:00Z', label: 'route USB',
    files: ['demo.kicad_pcb'], previewUrl: `/api/projects/p1/history/${B}/preview`,
  },
  {
    id: A, runId: 'r1', createdAt: '2026-10-16T07:00:00Z', label: 'place R1',
    files: ['demo.kicad_pcb'], previewUrl: null,
  },
];

describe('HistoryPanel', () => {
  beforeEach(() => {
    chat.runActive = false;
    apiGet.mockReset().mockResolvedValue(entries);
    apiPost.mockReset().mockResolvedValue({ id: B, files: [] });
  });

  it('is registered under id "history" (the iPhone History tab) together with git', async () => {
    await import('./index');
    const { result } = renderHook(() => usePanels());
    const ids = result.current.map((p) => p.id);
    expect(ids).toContain('history');
    expect(ids.indexOf('history')).toBeLessThan(ids.indexOf('git'));
  });

  it('lists turns newest first with preview thumbnails', async () => {
    render(<HistoryPanel projectId="p1" />);
    expect(await screen.findByText('route USB')).toBeInTheDocument();
    expect(apiGet).toHaveBeenCalledWith('/projects/p1/history');
    const img = screen.getByAltText('Preview of route USB') as HTMLImageElement;
    expect(img.getAttribute('src')).toBe(entries[0]!.previewUrl);
    expect(screen.queryByAltText('Preview of place R1')).toBeNull();
    const items = screen.getAllByRole('listitem');
    expect(items[0]).toHaveTextContent('route USB');
  });

  it('explains an empty history', async () => {
    apiGet.mockResolvedValue([]);
    render(<HistoryPanel projectId="p1" />);
    expect(await screen.findByText(/no snapshots yet/i)).toBeInTheDocument();
  });

  it('asks for confirmation before undo and restores on confirm', async () => {
    render(<HistoryPanel projectId="p1" />);
    fireEvent.click((await screen.findAllByRole('button', { name: /undo/i }))[0]!);
    expect(screen.getByRole('dialog')).toHaveTextContent('route USB');
    expect(apiPost).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }));
    expect(screen.queryByRole('dialog')).toBeNull();

    fireEvent.click(screen.getAllByRole('button', { name: /undo/i })[0]!);
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();

    fireEvent.click(screen.getAllByRole('button', { name: /undo/i })[0]!);
    fireEvent.click(screen.getByRole('button', { name: /^restore$/i }));
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith(`/projects/p1/history/${B}/restore`, {}));
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('shows the server error when restore is refused', async () => {
    apiPost.mockRejectedValue(new Error('project is open in KiCad; close it there before restoring'));
    render(<HistoryPanel projectId="p1" />);
    fireEvent.click((await screen.findAllByRole('button', { name: /undo/i }))[0]!);
    fireEvent.click(screen.getByRole('button', { name: /^restore$/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent('project is open in KiCad');
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('disables undo while the agent is running and reloads when the run ends', async () => {
    chat.runActive = true;
    const view = render(<HistoryPanel projectId="p1" />);
    const [undo] = await screen.findAllByRole('button', { name: /undo/i });
    expect(undo).toBeDisabled();
    expect(apiGet).toHaveBeenCalledTimes(1);
    chat.runActive = false;
    view.rerender(<HistoryPanel projectId="p1" />);
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(2));
    expect(screen.getAllByRole('button', { name: /undo/i })[0]).toBeEnabled();
  });
  it('clears a load error once a later reload succeeds', async () => {
    apiGet.mockRejectedValueOnce(new Error('HTTP 503'));
    chat.runActive = true;
    const view = render(<HistoryPanel projectId="p1" />);
    expect(await screen.findByRole('alert')).toHaveTextContent('HTTP 503');
    chat.runActive = false;
    view.rerender(<HistoryPanel projectId="p1" />);
    expect(await screen.findByText('route USB')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
