import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { docEntry } from '@/test/docs';
import { DocIndex } from './DocIndex';

const INDEX = {
  root: 'docs/design',
  docs: [
    docEntry('docs/design/requirements.md', { approved: true, approvedVersion: 'v1', lastAuthor: 'agent', lastConversationTitle: 'Session 2 — Requirements' }),
    docEntry('docs/design/architecture.md', { changedSinceApproval: true, approvedVersion: 'v0', userEdited: true, openPoints: 3, lastAuthor: 'user' }),
    docEntry('docs/design/parts.md', { openPoints: 1 }),
    docEntry('docs/design/decisions.md'),
    docEntry('docs/design/board.md', { exists: false, version: null }),
    docEntry('docs/design/notes/usb-pd.md', { fixed: false }),
  ],
  ignored: [{ path: 'docs/design/env.md', reason: 'symlink' }],
};

const row = (name: string) => screen.getByText(name).closest('button')!;

describe('DocIndex', () => {
  it('shows badges, the user-edited marker, open points and the last change', () => {
    render(<DocIndex index={INDEX} onOpen={vi.fn()} />);
    const names = within(screen.getByRole('list', { name: 'Design docs' }))
      .getAllByRole('button')
      .map((b) => b.querySelector('.font-mono')?.textContent);
    expect(names).toEqual(['requirements.md', 'architecture.md', 'parts.md', 'decisions.md', 'board.md', 'notes/usb-pd.md']);
    expect(within(row('requirements.md')).getByText('approved')).toBeInTheDocument();
    expect(within(row('requirements.md')).getByText('Last change: agent · Session 2 — Requirements')).toBeInTheDocument();
    expect(within(row('architecture.md')).getByText('changed since approval')).toBeInTheDocument();
    expect(within(row('architecture.md')).getByText('edited by you')).toBeInTheDocument();
    expect(within(row('architecture.md')).getByText('3 open points')).toBeInTheDocument();
    expect(within(row('architecture.md')).getByText('Last change: you')).toBeInTheDocument();
    expect(within(row('parts.md')).getByText('draft')).toBeInTheDocument();
    expect(within(row('parts.md')).getByText('1 open point')).toBeInTheDocument();
    expect(within(row('board.md')).getByText('missing')).toBeInTheDocument();
    expect(within(row('requirements.md')).queryByText('edited by you')).toBeNull();
  });

  it('lists ignored files with their reason and opens a doc', async () => {
    const onOpen = vi.fn();
    render(<DocIndex index={INDEX} onOpen={onOpen} />);
    expect(within(screen.getByRole('region', { name: 'Ignored files' })).getByText(/symlink/)).toBeInTheDocument();
    await userEvent.click(row('parts.md'));
    expect(onOpen).toHaveBeenCalledWith('docs/design/parts.md');
  });
});
