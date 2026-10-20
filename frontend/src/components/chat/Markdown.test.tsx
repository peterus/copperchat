import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { Markdown } from './Markdown';

describe('Markdown links', () => {
  it('without onRelativeLink every link opens in a new tab (chat)', () => {
    render(<Markdown text="[notes](../parts/LM317/notes.md) [web](https://example.com)" />);
    expect(screen.getByRole('link', { name: 'notes' })).toHaveAttribute('target', '_blank');
  });

  it('with onRelativeLink a resolved relative link runs its action, others are plain text', async () => {
    const action = vi.fn();
    const onRelativeLink = vi.fn((href: string) => (href.includes('parts') ? action : null));
    render(
      <Markdown
        text="[notes](../parts/LM317/notes.md) [other](./architecture.md) [api](/api/x) [web](https://example.com)"
        onRelativeLink={onRelativeLink}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: 'notes' }));
    expect(action).toHaveBeenCalledOnce();
    expect(screen.getByText('other').closest('a')).toBeNull();
    expect(screen.queryByRole('button', { name: 'other' })).toBeNull();
    expect(screen.getByText('api').closest('a')).toBeNull();
    expect(screen.getByRole('link', { name: 'web' })).toHaveAttribute('href', 'https://example.com');
  });
});
