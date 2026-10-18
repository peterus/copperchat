import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/api/http';

const apiGet = vi.fn();
const apiPut = vi.fn();
vi.mock('@/api/http', async (orig) => ({
  ...(await orig<typeof import('@/api/http')>()),
  apiGet: (...a: unknown[]) => apiGet(...a),
  apiPut: (...a: unknown[]) => apiPut(...a),
}));

import { type OpenProject, useProjects } from '@/stores/projects';
import { EditingSection } from './EditingSection';

const PID = 'a1b2c3d4e5f6';
const DEFAULTS = { planPolicy: 'large', changeBudget: 20, annotationBlock: 100, showAgentView: true };
const project: OpenProject = {
  id: PID, name: 'demo', relPath: 'demo/demo.kicad_pro', hasSch: true, hasPcb: false, locked: false,
  readOnly: false, lockFiles: [], kicadVersion: '10.99.0', viewFiles: { sch: [], pcb: [] }, viewBytes: {},
};
const URL = `/projects/${PID}/settings/editing`;

describe('EditingSection', () => {
  beforeEach(() => {
    apiGet.mockReset().mockResolvedValue(DEFAULTS);
    apiPut.mockReset().mockImplementation(async (_p: string, body: object) => ({ ...DEFAULTS, ...body }));
    useProjects.setState({ current: project });
  });
  afterEach(() => useProjects.setState({ current: null }));

  it('without an open project it asks to open one', () => {
    useProjects.setState({ current: null });
    render(<EditingSection />);
    expect(screen.getByRole('heading', { name: 'Editing' })).toBeInTheDocument();
    expect(screen.getByText('Open a project to change its editing settings.')).toBeInTheDocument();
    expect(apiGet).not.toHaveBeenCalled();
  });

  it('shows the current project settings with their defaults', async () => {
    render(<EditingSection />);
    expect(await screen.findByRole('radio', { name: 'For large changes' })).toBeChecked();
    expect(apiGet).toHaveBeenCalledWith(URL);
    expect(screen.getByText(/demo/)).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: 'Always' })).not.toBeChecked();
    expect(screen.getByRole('radio', { name: 'Never' })).not.toBeChecked();
    const budget = screen.getByRole('spinbutton', { name: /Change budget/ });
    expect(budget).toHaveValue(20);
    expect(budget).toHaveAttribute('min', '1');
    expect(budget).toHaveAttribute('max', '500');
    expect(screen.getByRole('radio', { name: '×100' })).toBeChecked();
    expect(screen.getByRole('radio', { name: '×1000' })).not.toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Show agent view after each step' })).toBeChecked();
  });

  it('a change PUTs only that field', async () => {
    render(<EditingSection />);
    fireEvent.click(await screen.findByRole('radio', { name: 'Always' }));
    await waitFor(() => expect(apiPut).toHaveBeenCalledWith(URL, { planPolicy: 'always' }));
    fireEvent.click(screen.getByRole('radio', { name: '×1000' }));
    await waitFor(() => expect(apiPut).toHaveBeenLastCalledWith(URL, { annotationBlock: 1000 }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Show agent view after each step' }));
    await waitFor(() => expect(apiPut).toHaveBeenLastCalledWith(URL, { showAgentView: false }));
    const budget = screen.getByRole('spinbutton', { name: /Change budget/ });
    fireEvent.change(budget, { target: { value: '35' } });
    expect(apiPut).toHaveBeenCalledTimes(3); // typing does not save
    fireEvent.blur(budget);
    await waitFor(() => expect(apiPut).toHaveBeenLastCalledWith(URL, { changeBudget: 35 }));
    expect(apiPut).toHaveBeenCalledTimes(4);
    fireEvent.blur(budget); // unchanged: no second PUT
    expect(apiPut).toHaveBeenCalledTimes(4);
  });

  it('an out-of-range budget is refused locally and reset', async () => {
    render(<EditingSection />);
    const budget = await screen.findByRole('spinbutton', { name: /Change budget/ });
    fireEvent.change(budget, { target: { value: '0' } });
    fireEvent.keyDown(budget, { key: 'Enter' });
    expect(await screen.findByRole('alert')).toHaveTextContent('between 1 and 500');
    expect(apiPut).not.toHaveBeenCalled();
    fireEvent.change(budget, { target: { value: '21' } });
    fireEvent.keyDown(budget, { key: 'Enter' });
    await waitFor(() => expect(apiPut).toHaveBeenCalledWith(URL, { changeBudget: 21 }));
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  });

  it('a server 400 is shown inline and the saved value comes back', async () => {
    apiPut.mockRejectedValue(new ApiError(400, { error: { code: 'invalid_argument', message: 'plan_policy must be one of always, large, never' } }, 'plan_policy must be one of always, large, never'));
    render(<EditingSection />);
    fireEvent.click(await screen.findByRole('radio', { name: 'Never' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('plan_policy must be one of always, large, never');
    expect(screen.getByRole('radio', { name: 'For large changes' })).toBeChecked();
  });

  it('a load failure is shown', async () => {
    apiGet.mockRejectedValue(new Error('HTTP 503'));
    render(<EditingSection />);
    expect(await screen.findByRole('alert')).toHaveTextContent('HTTP 503');
  });

  it('a failing save superseded by a newer one still shows its error and the confirmed state', async () => {
    let failFirst: (e: Error) => void = () => {};
    let finishSecond: (v: object) => void = () => {};
    apiPut
      .mockImplementationOnce(() => new Promise((_r, rej) => (failFirst = rej)))
      .mockImplementationOnce(() => new Promise((r) => (finishSecond = r)));
    render(<EditingSection />);
    fireEvent.click(await screen.findByRole('radio', { name: 'Always' }));
    fireEvent.click(screen.getByRole('radio', { name: '×1000' }));
    failFirst(new Error('database is locked'));
    expect(await screen.findByRole('alert')).toHaveTextContent('database is locked');
    // the server merged only the second field: its response is the truth
    finishSecond({ ...DEFAULTS, annotationBlock: 1000 });
    await waitFor(() => expect(screen.getByRole('radio', { name: 'For large changes' })).toBeChecked());
    expect(screen.getByRole('radio', { name: '×1000' })).toBeChecked();
    expect(screen.getByRole('alert')).toHaveTextContent('database is locked');
  });

  it('the newest save failing before an older one succeeds still ends on the server state', async () => {
    let finishFirst: (v: object) => void = () => {};
    let failSecond: (e: Error) => void = () => {};
    apiPut
      .mockImplementationOnce(() => new Promise((r) => (finishFirst = r)))
      .mockImplementationOnce(() => new Promise((_r, rej) => (failSecond = rej)));
    render(<EditingSection />);
    fireEvent.click(await screen.findByRole('radio', { name: 'Always' }));
    fireEvent.click(screen.getByRole('radio', { name: '×1000' }));
    failSecond(new Error('database is locked'));
    expect(await screen.findByRole('alert')).toHaveTextContent('database is locked');
    finishFirst({ ...DEFAULTS, planPolicy: 'always' });
    await waitFor(() => expect(screen.getByRole('radio', { name: '×100' })).toBeChecked());
    expect(screen.getByRole('radio', { name: 'Always' })).toBeChecked();
  });
});
