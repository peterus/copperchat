import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { MessageItem } from '@/components/chat/MessageItem';
import type { MessageDTO } from '@/protocol/frames';
import { useUi } from '@/stores/ui';
import { docChangedOf } from './DocChangedCard';

const notice = (metadata: Record<string, unknown>): MessageDTO => ({
  id: 7, projectId: 'a1b2c3d4e5f6', conversationId: 'c'.repeat(32), clientMessageId: null, runId: 'r1', role: 'system',
  kind: 'notice', content: 'requirements.md updated', status: 'done', metadata, createdAt: '2026-10-19T10:00:00Z',
});
const MD = {
  action: 'doc-changed', path: 'docs/design/requirements.md', revisionId: 12, added: 12, removed: 3,
  sections: ['Interfaces', 'Open points'],
};

beforeEach(() => useUi.setState({ panelRequest: null }));

describe('doc-changed notice', () => {
  it('MessageItem renders the card: path, +/−, sections; View diff opens the Docs panel', async () => {
    render(<MessageItem message={notice(MD)} />);
    expect(screen.getByText('requirements.md')).toBeInTheDocument();
    expect(screen.getByText('+12')).toBeInTheDocument();
    expect(screen.getByText('−3')).toBeInTheDocument();
    expect(screen.getByText('(Interfaces, Open points)')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'View diff' }));
    expect(useUi.getState().panelRequest).toEqual({
      id: 'docs',
      params: { path: 'docs/design/requirements.md', revisionId: '12' },
    });
  });

  it('a message-delta metadata patch updates the card in place', () => {
    const { rerender } = render(<MessageItem message={notice(MD)} />);
    rerender(<MessageItem message={notice({ ...MD, revisionId: 13, added: 20, removed: 4, sections: ['Interfaces', 'Power'] })} />);
    expect(screen.getByText('+20')).toBeInTheDocument();
    expect(screen.getByText('(Interfaces, Power)')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'View diff' })).toHaveLength(1);
  });

  it('a malformed notice falls back to the plain notice text', () => {
    render(<MessageItem message={notice({ action: 'doc-changed' })} />);
    expect(screen.getByText('requirements.md updated')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'View diff' })).toBeNull();
  });

  it('parses defensively', () => {
    expect(docChangedOf({ ...MD, revisionId: 'x', added: -1, sections: ['A', 3] })).toEqual({
      path: 'docs/design/requirements.md', revisionId: null, added: 0, removed: 3, sections: ['A'],
    });
  });
});
