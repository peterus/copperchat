import { act, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { registerPanel, usePanels } from './panels';

const Icon = () => <svg />;

function List() {
  return (
    <ul>
      {usePanels().map((p) => (
        <li key={p.id}>{p.title}</li>
      ))}
    </ul>
  );
}

describe('panel registry', () => {
  it('registers, orders, replaces and unregisters panels reactively', () => {
    render(<List />);
    const offs: Array<() => void> = [];
    act(() => {
      offs.push(registerPanel({ id: 'b', title: 'Parts', icon: Icon, component: () => null, order: 20 }));
      offs.push(registerPanel({ id: 'a', title: 'History', icon: Icon, component: () => null, order: 10 }));
      offs.push(registerPanel({ id: 'c', title: 'Alpha', icon: Icon, component: () => null }));
    });
    // order first (default 100), then title
    expect(screen.getAllByRole('listitem').map((li) => li.textContent)).toEqual(['History', 'Parts', 'Alpha']);
    act(() => {
      offs.push(registerPanel({ id: 'b', title: 'Parts v2', icon: Icon, component: () => null, order: 20 }));
    });
    expect(screen.getByText('Parts v2')).toBeInTheDocument();
    expect(screen.queryByText('Parts')).toBeNull();
    // the replaced definition's unregister is a no-op
    act(() => offs[0]?.());
    expect(screen.getByText('Parts v2')).toBeInTheDocument();
    act(() => offs[1]?.());
    expect(screen.queryByText('History')).toBeNull();
    act(() => offs.forEach((f) => f()));
    expect(screen.queryAllByRole('listitem')).toHaveLength(0);
  });
});
