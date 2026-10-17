import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ProjectPickerPage } from '@/pages/ProjectPickerPage';
import { useProjects } from './projects';

const list = [
  { id: 'a1b2c3d4e5f6', name: 'demo', relPath: 'demo/demo.kicad_pro', hasSch: true, hasPcb: true, locked: false },
  { id: 'ffffffffffff', name: 'busy', relPath: 'lab/busy.kicad_pro', hasSch: true, hasPcb: false, locked: true },
];

const busyOpen = {
  id: 'ffffffffffff',
  readOnly: true,
  lockFiles: ['~busy.kicad_sch.lck'],
  kicadVersion: '10.99.0',
  viewFiles: { sch: ['busy.kicad_sch'], pcb: [] },
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

beforeEach(() => {
  useProjects.getState().reset();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url === '/api/projects') return json(list);
      if (url === '/api/projects/ffffffffffff/open') return json(busyOpen);
      if (url === '/api/projects/a1b2c3d4e5f6/open') return json({ detail: 'KiCad is not available' }, 503);
      return new Response(null, { status: 404 });
    }),
  );
});

function renderPicker() {
  return render(
    <MemoryRouter initialEntries={['/projects']}>
      <Routes>
        <Route path="/projects" element={<ProjectPickerPage />} />
        <Route path="/p/:projectId" element={<p>project page</p>} />
        <Route path="/settings" element={<p>settings page</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('projects', () => {
  it('picker lists projects, warns about locks and navigates on click', async () => {
    renderPicker();
    expect(await screen.findByText('demo')).toBeInTheDocument();
    expect(screen.getByText('lab/busy.kicad_pro')).toBeInTheDocument();
    expect(screen.getAllByText(/Open in KiCad — close it there/)).toHaveLength(1);
    fireEvent.change(screen.getByRole('searchbox', { name: 'Filter projects' }), { target: { value: 'bus' } });
    expect(screen.queryByText('demo')).toBeNull();
    fireEvent.click(screen.getByRole('link', { name: /busy/ }));
    expect(await screen.findByText('project page')).toBeInTheDocument();
  });

  it('picker shows a load error', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ detail: 'PROJECTS_ROOT missing' }, 500)));
    renderPicker();
    expect(await screen.findByRole('alert')).toHaveTextContent('PROJECTS_ROOT missing');
  });

  it('open() merges the summary with the open result incl. viewFiles (loading the list if needed)', async () => {
    const opened = await useProjects.getState().open('ffffffffffff');
    expect(opened).toMatchObject({
      name: 'busy',
      hasPcb: false,
      readOnly: true,
      lockFiles: ['~busy.kicad_sch.lck'],
      viewFiles: { sch: ['busy.kicad_sch'], pcb: [] },
    });
    expect(useProjects.getState()).toMatchObject({ opening: null, openError: null });
    expect(useProjects.getState().current?.id).toBe('ffffffffffff');
  });

  it('a failed open reports the error and leaves no current project', async () => {
    const opened = await useProjects.getState().open('a1b2c3d4e5f6');
    expect(opened).toBeNull();
    expect(useProjects.getState()).toMatchObject({ current: null, opening: null, openError: 'KiCad is not available' });
  });

  it('a stale open result does not overwrite a newer project switch', async () => {
    let releaseSlow: () => void = () => {};
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === '/api/projects') return json(list);
        if (url === '/api/projects/a1b2c3d4e5f6/open') {
          await new Promise<void>((r) => (releaseSlow = r));
          return json({ ...busyOpen, id: 'a1b2c3d4e5f6', readOnly: false, lockFiles: [] });
        }
        if (url === '/api/projects/ffffffffffff/open') return json(busyOpen);
        return new Response(null, { status: 404 });
      }),
    );
    await useProjects.getState().load();
    const slow = useProjects.getState().open('a1b2c3d4e5f6');
    await useProjects.getState().open('ffffffffffff');
    releaseSlow();
    expect(await slow).toBeNull();
    expect(useProjects.getState().current?.id).toBe('ffffffffffff');
  });

  it('reset() drops the list and the current project', async () => {
    await useProjects.getState().open('ffffffffffff');
    useProjects.getState().reset();
    expect(useProjects.getState()).toMatchObject({ projects: [], current: null, opening: null, error: null });
  });
});
