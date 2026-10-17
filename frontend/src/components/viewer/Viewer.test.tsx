import { act, fireEvent, render, screen } from '@testing-library/react';
import { useEffect } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useComposer } from '@/stores/composer';
import { type OpenProject, useProjects } from '@/stores/projects';
import { createViewStore, ViewStoreContext } from '@/stores/view';
import { DEFAULT_VIEWER_LIMITS, useViewerPrefs } from '@/stores/viewerPrefs';
import { FakeWsClient } from '@/test/fakeWsClient';
import { setMedia } from '@/test/media';
import type { KicadPaneProps } from './types';
import { Viewer } from './Viewer';

const PID = 'a1b2c3d4e5f6';
const mounts: string[] = [];

vi.mock('./KicadPane', () => ({
  KicadPane: function FakeKicadPane(p: KicadPaneProps) {
    // eslint-disable-next-line react-hooks/exhaustive-deps
    useEffect(() => void mounts.push(p.doc), []);
    return (
      <div data-testid={`kicad-pane-${p.doc}`} data-rev={String(p.rev)} data-theme={p.theme} data-files={p.files.join(',')}>
        <button type="button" onClick={() => p.onPick('R12')}>
          pick R12 on {p.doc}
        </button>
        {p.find && (
          <button type="button" onClick={() => p.onFindResult?.(p.find!.ref, false)}>
            find {p.find.ref}
          </button>
        )}
      </div>
    );
  },
}));

let ws: FakeWsClient;

function openProject(over: Partial<OpenProject> = {}) {
  const current: OpenProject = {
    id: PID,
    name: 'demo',
    relPath: 'demo/demo.kicad_pro',
    hasSch: true,
    hasPcb: true,
    locked: false,
    readOnly: false,
    lockFiles: [],
    kicadVersion: '10.99',
    viewFiles: { sch: ['demo.kicad_sch', 'power.kicad_sch'], pcb: ['demo.kicad_pcb'] },
    viewBytes: { 'demo.kicad_sch': 50_000, 'power.kicad_sch': 20_000, 'demo.kicad_pcb': 160_000 },
    ...over,
  };
  useProjects.setState({ projects: [current], current });
}

function renderViewer() {
  ws = new FakeWsClient();
  const store = createViewStore(ws);
  render(
    <ViewStoreContext.Provider value={store}>
      <Viewer projectId={PID} />
    </ViewStoreContext.Provider>,
  );
  return store;
}

const pane = (doc: 'sch' | 'pcb') => screen.getByTestId(`kicad-pane-${doc}`);
const agentImg = () => screen.getByRole('img', { name: /Agent view/ });

beforeEach(() => {
  mounts.length = 0;
  setMedia(() => false);
  useComposer.getState().clear();
  useViewerPrefs.setState({ limits: { ...DEFAULT_VIEWER_LIMITS } });
  openProject();
});

describe('Viewer', () => {
  it('switches between Schematic and PCB tabs and keeps visited panes mounted', () => {
    renderViewer();
    expect(screen.getByRole('tab', { name: 'Schematic' })).toHaveAttribute('aria-selected', 'true');
    expect(pane('sch').dataset.files).toBe('demo.kicad_sch,power.kicad_sch');
    fireEvent.click(screen.getByRole('tab', { name: 'PCB' }));
    expect(screen.getByRole('tab', { name: 'PCB' })).toHaveAttribute('aria-selected', 'true');
    expect(pane('pcb').dataset.files).toBe('demo.kicad_pcb');
    fireEvent.click(screen.getByRole('tab', { name: 'Schematic' }));
    fireEvent.click(screen.getByRole('button', { name: 'Agent view' }));
    fireEvent.click(screen.getByRole('button', { name: 'KiCad view' }));
    expect(mounts.filter((d) => d === 'sch')).toHaveLength(1); // not re-created (no re-parse)
    expect(pane('sch').closest('[hidden]')).toBeNull();
    expect(pane('pcb').closest('[hidden]')).not.toBeNull();
  });

  it('reloads the KiCad pane and the agent view on view-updated, ignoring other projects', () => {
    renderViewer();
    fireEvent.click(screen.getByRole('tab', { name: 'PCB' }));
    expect(pane('pcb').dataset.rev).toBe('null');
    act(() => ws.emit({ type: 'view-updated', projectId: PID, docs: ['pcb'], version: 6 }));
    expect(pane('pcb').dataset.rev).toBe('1');
    act(() => ws.emit({ type: 'view-updated', projectId: '0123456789ab', docs: ['pcb'], version: 9 }));
    expect(pane('pcb').dataset.rev).toBe('1');
    fireEvent.click(screen.getByRole('button', { name: 'Agent view' }));
    expect(agentImg().getAttribute('src')).toContain('&v=1');
    act(() => ws.emit({ type: 'view-updated', projectId: PID, docs: ['pcb'], version: 7 }));
    expect(agentImg().getAttribute('src')).toContain('&v=2');
  });

  it('inserts @REF into the composer when a part is picked', () => {
    renderViewer();
    fireEvent.click(screen.getByRole('button', { name: 'pick R12 on sch' }));
    expect(useComposer.getState().text).toBe('@R12 ');
  });

  it('shows layer toggles only for the PCB agent view and puts them in the URL', () => {
    renderViewer();
    fireEvent.click(screen.getByRole('button', { name: 'Agent view' }));
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
    fireEvent.click(screen.getByRole('tab', { name: 'PCB' }));
    expect(screen.getAllByRole('checkbox')).toHaveLength(8);
    fireEvent.click(screen.getByRole('checkbox', { name: 'B.Cu' }));
    expect(agentImg().getAttribute('src')).toContain('layers=F.Cu,F.SilkS,B.SilkS,Edge.Cuts');
    fireEvent.click(screen.getByRole('checkbox', { name: 'F.Fab' }));
    expect(agentImg().getAttribute('src')).toContain('layers=F.Cu,F.SilkS,B.SilkS,Edge.Cuts,F.Fab');
    fireEvent.click(screen.getByRole('button', { name: 'KiCad view' }));
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
  });

  it('passes the app theme to the panes', () => {
    renderViewer();
    expect(['light', 'dark']).toContain(pane('sch').dataset.theme);
  });

  it('finds a reference via the search box and reports a miss', () => {
    renderViewer();
    fireEvent.click(screen.getByRole('button', { name: 'Agent view' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Find reference' }), { target: { value: ' U3 ' } });
    fireEvent.submit(screen.getByRole('search'));
    expect(screen.getByRole('button', { name: 'KiCad view' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: 'find U3' }));
    expect(screen.getByText('U3 not found in the schematic.')).toBeInTheDocument();
  });

  it('enters and leaves fullscreen', () => {
    renderViewer();
    expect(screen.getByRole('region', { name: 'Viewer' }).className).not.toContain('fixed');
    fireEvent.click(screen.getByRole('button', { name: 'Fullscreen' }));
    expect(screen.getByRole('region', { name: 'Viewer' }).className).toContain('fixed');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.getByRole('region', { name: 'Viewer' }).className).not.toContain('fixed');
  });

  it('says so when the project has no board', () => {
    openProject({ hasPcb: false, viewFiles: { sch: ['demo.kicad_sch'], pcb: [] } });
    renderViewer();
    fireEvent.click(screen.getByRole('tab', { name: 'PCB' }));
    expect(screen.getByText('This project has no board.')).toBeInTheDocument();
  });

  it('Find on a missing document says so', () => {
    openProject({ hasPcb: false, viewFiles: { sch: ['demo.kicad_sch'], pcb: [] } });
    renderViewer();
    fireEvent.click(screen.getByRole('tab', { name: 'PCB' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Find reference' }), { target: { value: 'U1' } });
    fireEvent.submit(screen.getByRole('search'));
    expect(screen.getByRole('status')).toHaveTextContent('This project has no board.');
  });

  it('says the view could not be prepared when the board exists but has no snapshot', () => {
    openProject({ viewFiles: { sch: ['demo.kicad_sch'], pcb: [] } });
    renderViewer();
    fireEvent.click(screen.getByRole('tab', { name: 'PCB' }));
    expect(screen.getByText(/interactive board view could not be prepared/)).toBeInTheDocument();
  });

  describe('large-board guard', () => {
    const big = { viewBytes: { 'demo.kicad_sch': 50_000, 'power.kicad_sch': 20_000, 'demo.kicad_pcb': 85_500_000 } };

    it('on a phone shows the agent view with "load interactive view anyway"', () => {
      setMedia((q) => q === '(pointer: coarse)');
      openProject(big);
      renderViewer();
      fireEvent.click(screen.getByRole('tab', { name: 'PCB' }));
      expect(screen.queryByTestId('kicad-pane-pcb')).toBeNull();
      expect(screen.getByRole('note')).toHaveTextContent('85.5 MB, limit 15 MB on this device');
      expect(screen.getByRole('note')).toHaveTextContent(/may crash this browser tab/);
      expect(agentImg().getAttribute('src')).toContain('doc=pcb');
      fireEvent.click(screen.getByRole('button', { name: 'Load interactive view anyway' }));
      expect(pane('pcb')).toBeInTheDocument();
    });

    it('on a desktop warns about load time before loading', () => {
      openProject(big);
      renderViewer();
      fireEvent.click(screen.getByRole('tab', { name: 'PCB' }));
      expect(screen.getByRole('note')).toHaveTextContent(/limit 60 MB.*may take minutes/);
      expect(pane('sch')).toBeInTheDocument(); // the small schematic is not guarded
    });

    it('explains that Find needs the interactive view while the guard is shown', () => {
      openProject(big);
      renderViewer();
      fireEvent.click(screen.getByRole('tab', { name: 'PCB' }));
      fireEvent.change(screen.getByRole('textbox', { name: 'Find reference' }), { target: { value: 'U1' } });
      fireEvent.submit(screen.getByRole('search'));
      expect(screen.getByText('Find needs the interactive view, which is not loaded.')).toBeInTheDocument();
    });

    it('follows the per-device limit from the settings', () => {
      openProject(big);
      useViewerPrefs.setState({ limits: { mobileMb: 15, desktopMb: 100 } });
      renderViewer();
      fireEvent.click(screen.getByRole('tab', { name: 'PCB' }));
      expect(pane('pcb')).toBeInTheDocument();
    });
  });

  it('never says "Claude Code"', () => {
    renderViewer();
    expect(document.body.textContent).not.toMatch(/Claude Code/);
  });
});
