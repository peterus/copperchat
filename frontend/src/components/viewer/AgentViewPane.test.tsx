import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { AgentViewPane } from './AgentViewPane';
import { LayerToggles } from './LayerToggles';

const surface = () => screen.getByTestId('agent-view-surface');
const transform = () => (surface().firstElementChild as HTMLElement).style.transform;

function tap(x: number, y: number) {
  fireEvent.pointerDown(surface(), { pointerId: 1, clientX: x, clientY: y });
  fireEvent.pointerUp(surface(), { pointerId: 1, clientX: x, clientY: y });
}

describe('AgentViewPane', () => {
  it('requests the agent view with theme, layers and reload counter', () => {
    render(<AgentViewPane projectId="p1" doc="pcb" rev={4} theme="light" layers={['F.Cu', 'Edge.Cuts']} />);
    expect(screen.getByRole('img', { name: 'Agent view of the PCB' }).getAttribute('src')).toBe(
      '/api/projects/p1/agent-view?doc=pcb&theme=light&layers=F.Cu,Edge.Cuts&v=4',
    );
    expect(screen.getByRole('status')).toHaveTextContent('Rendering');
    fireEvent.load(screen.getByRole('img', { name: 'Agent view of the PCB' }));
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('shows an error with a retry when the render fails', () => {
    render(<AgentViewPane projectId="p1" doc="sch" rev={null} theme="dark" layers={[]} />);
    const img = screen.getByRole('img', { name: 'Agent view of the schematic' });
    fireEvent.error(img);
    expect(screen.getByRole('alert')).toHaveTextContent('Render failed');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByRole('img', { name: 'Agent view of the schematic' })).not.toBe(img);
  });

  it('zooms with the buttons and fits again', () => {
    render(<AgentViewPane projectId="p1" doc="pcb" rev={1} theme="dark" layers={[]} />);
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    expect(transform()).toContain('scale(2)');
    fireEvent.click(screen.getByRole('button', { name: 'Zoom out' }));
    expect(transform()).toContain('scale(1)');
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    fireEvent.click(screen.getByRole('button', { name: 'Fit to view' }));
    expect(transform()).toBe('translate(0px, 0px) scale(1)');
  });

  it('double tap zooms in about the (second) tap and a second double tap fits', () => {
    render(<AgentViewPane projectId="p1" doc="pcb" rev={1} theme="dark" layers={[]} />);
    tap(40, 30);
    tap(41, 30);
    expect(transform()).toBe('translate(-41px, -30px) scale(2)');
    tap(10, 10);
    tap(10, 10);
    expect(transform()).toBe('translate(0px, 0px) scale(1)');
  });

  it('drags to pan', () => {
    render(<AgentViewPane projectId="p1" doc="pcb" rev={1} theme="dark" layers={[]} />);
    fireEvent.pointerDown(surface(), { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(surface(), { pointerId: 1, clientX: 30, clientY: 15 });
    fireEvent.pointerUp(surface(), { pointerId: 1, clientX: 30, clientY: 15 });
    expect(transform()).toBe('translate(20px, 5px) scale(1)');
  });
});

describe('LayerToggles', () => {
  it('renders eight checkboxes reflecting the selection and reports toggles', () => {
    const onToggle = vi.fn();
    render(<LayerToggles layers={['F.Cu']} onToggle={onToggle} />);
    expect(screen.getAllByRole('checkbox')).toHaveLength(8);
    expect(screen.getByRole('checkbox', { name: 'F.Cu' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'B.Cu' })).not.toBeChecked();
    fireEvent.click(screen.getByRole('checkbox', { name: 'User.Drawings' }));
    expect(onToggle).toHaveBeenCalledWith('User.Drawings');
  });
});
