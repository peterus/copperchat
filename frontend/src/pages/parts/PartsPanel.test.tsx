import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const http = vi.hoisted(() => ({ apiGet: vi.fn(), apiPut: vi.fn() }));
vi.mock('@/api/http', async (importOriginal) => ({ ...(await importOriginal<object>()), ...http }));

import { ApiError } from '@/api/http';
import { stripFrontMatter } from '@/api/parts';
import { PartsPanel } from './PartsPanel';

const part = { mpn: 'TPS62133', hasNotes: true, hasDatasheet: true, userEdited: false, updatedAt: null };

beforeEach(() => {
  vi.resetAllMocks();
  http.apiGet.mockImplementation(async (path: string) =>
    path.endsWith('/parts')
      ? [part, { ...part, mpn: 'LM317', hasDatasheet: false, userEdited: true }]
      : {
          mpn: path.includes('LM317') ? 'LM317' : 'TPS62133',
          content: '---\nuser_edited: false\n---\n# Pinout\nVIN on pin 1',
          userEdited: false,
        },
  );
  http.apiPut.mockResolvedValue(undefined);
});

describe('PartsPanel', () => {
  it('renders notes, links the datasheet and saves edits', async () => {
    render(<PartsPanel projectId="p1" />);
    await userEvent.click(await screen.findByText('TPS62133'));
    expect(await screen.findByRole('heading', { name: 'Pinout' })).toBeInTheDocument();
    expect(screen.queryByText(/user_edited/)).toBeNull();
    const link = screen.getByRole('link', { name: /datasheet/i });
    expect(link).toHaveAttribute('href', '/api/projects/p1/parts/TPS62133/datasheet');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    await userEvent.click(screen.getByRole('button', { name: 'Edit' }));
    expect(screen.getByText(/researcher will not overwrite/i)).toBeInTheDocument();
    const box = screen.getByRole('textbox', { name: 'Notes' });
    await userEvent.clear(box);
    await userEvent.type(box, 'mine');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(http.apiPut).toHaveBeenCalledWith('/projects/p1/parts/TPS62133/notes', { content: 'mine' }),
    );
    await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Notes' })).toBeNull());
  });

  it('cancel discards the draft; no datasheet link without a datasheet; edited tag', async () => {
    render(<PartsPanel projectId="p1" />);
    expect(await screen.findByText('edited by you')).toBeInTheDocument();
    await userEvent.click(screen.getByText('LM317'));
    await screen.findByRole('heading', { name: 'Pinout' });
    expect(screen.queryByRole('link', { name: /datasheet/i })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Edit' }));
    await userEvent.type(screen.getByRole('textbox', { name: 'Notes' }), 'x');
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('textbox', { name: 'Notes' })).toBeNull();
    expect(http.apiPut).not.toHaveBeenCalled();
  });

  it('shows an empty state', async () => {
    http.apiGet.mockResolvedValue([]);
    render(<PartsPanel projectId="p1" />);
    expect(await screen.findByText(/no researched parts yet/i)).toBeInTheDocument();
  });

  it('shows save errors as an alert', async () => {
    http.apiPut.mockRejectedValue(new ApiError(400, { detail: 'too large' }, 'too large'));
    render(<PartsPanel projectId="p1" />);
    await userEvent.click(await screen.findByText('TPS62133'));
    await userEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('too large');
  });
});

describe('stripFrontMatter', () => {
  it('removes a leading YAML block only', () => {
    expect(stripFrontMatter('---\na: 1\n---\n# T')).toBe('# T');
    expect(stripFrontMatter('# T\n---\na\n---\n')).toBe('# T\n---\na\n---\n');
    expect(stripFrontMatter('---\r\na: 1\r\n---\r\nx')).toBe('x');
  });
});

it('a slow list of the previous project never replaces the new one', async () => {
  let releaseA: (v: unknown) => void = () => {};
  http.apiGet.mockImplementation(async (path: string) => {
    if (path === '/projects/pA/parts') return new Promise((r) => (releaseA = r));
    if (path === '/projects/pB/parts') return [{ mpn: 'B-PART', hasNotes: false, hasDatasheet: false, userEdited: false, updatedAt: null }];
    return { mpn: 'X', content: '', userEdited: false };
  });
  const { rerender } = render(<PartsPanel projectId="pA" />);
  rerender(<PartsPanel projectId="pB" />);
  expect(await screen.findByText('B-PART')).toBeInTheDocument();
  releaseA([{ mpn: 'A-PART', hasNotes: false, hasDatasheet: false, userEdited: false, updatedAt: null }]);
  await new Promise((r) => setTimeout(r, 20));
  expect(screen.queryByText('A-PART')).not.toBeInTheDocument();
  expect(screen.getByText('B-PART')).toBeInTheDocument();
});
