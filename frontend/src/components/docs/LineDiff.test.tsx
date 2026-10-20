import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { LineDiff } from './LineDiff';
import * as lineDiff from './lineDiff';

const lines = (n: number, p = 'l') => Array.from({ length: n }, (_, i) => `${p}${i}`);

describe('LineDiff', () => {
  it('renders a unified diff: additions, removals, folded context that expands', async () => {
    const before = [...lines(10), 'old', ...lines(10, 'm')].join('\n');
    const after = [...lines(10), 'new', ...lines(10, 'm')].join('\n');
    render(<LineDiff before={before} after={after} />);
    const rows = screen.getAllByRole('row');
    expect(rows.filter((r) => r.dataset.op === 'del').map((r) => r.textContent)).toEqual(['- old']);
    expect(rows.filter((r) => r.dataset.op === 'add').map((r) => r.textContent)).toEqual(['+ new']);
    expect(rows.find((r) => r.dataset.op === 'add')?.className).toMatch(/success/);
    expect(rows.find((r) => r.dataset.op === 'del')?.className).toMatch(/danger/);
    expect(screen.queryByText('l0')).toBeNull();
    await userEvent.click(screen.getAllByRole('button', { name: '⋯ 7 unchanged lines' })[0]!);
    expect(screen.getByText('l0')).toBeInTheDocument();
  });

  it('says so when there is nothing or too much to diff', () => {
    const { rerender } = render(<LineDiff before="a" after="a" />);
    expect(screen.getByText('No changes.')).toBeInTheDocument();
    const spy = vi.spyOn(lineDiff, 'diffLines').mockReturnValue({ tooLarge: true, stats: { d: 2001, allocated: 0 } });
    rerender(<LineDiff before="x" after="y" />);
    expect(screen.getByText('Too large to diff.')).toBeInTheDocument();
    spy.mockRestore();
  });

  it('a change of line endings only is not called "No changes"', () => {
    render(<LineDiff before={'a\nb\n'} after={'a\r\nb'} />);
    expect(screen.getByText('Only line endings or the final newline changed.')).toBeInTheDocument();
  });

  it('new inputs start folded again', async () => {
    const mk = (mid: string) => [...lines(10), mid, ...lines(10, 'm')].join('\n');
    const { rerender } = render(<LineDiff before={mk('a')} after={mk('b')} />);
    await userEvent.click(screen.getAllByRole('button', { name: '⋯ 7 unchanged lines' })[0]!);
    expect(screen.getByText('l0')).toBeInTheDocument();
    rerender(<LineDiff before={mk('a')} after={mk('c')} />);
    expect(screen.queryByText('l0')).toBeNull();
  });
});
