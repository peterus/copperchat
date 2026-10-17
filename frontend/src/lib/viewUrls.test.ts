import { describe, expect, it } from 'vitest';
import { agentViewUrl, DEFAULT_LAYERS, PCB_LAYERS, viewFileUrl } from './viewUrls';

describe('viewFileUrl', () => {
  it('adds the reload counter as cache buster', () => {
    expect(viewFileUrl('p 1', 'demo.kicad_pcb', 4)).toBe('/api/projects/p%201/view/files/demo.kicad_pcb?v=4');
  });
  it('omits v before the first view-updated', () => {
    expect(viewFileUrl('p1', 'demo.kicad_sch', null)).toBe('/api/projects/p1/view/files/demo.kicad_sch');
  });
  it('encodes each path segment of a sub-sheet but keeps the slashes', () => {
    expect(viewFileUrl('p1', 'sheets/power #1.kicad_sch', null)).toBe(
      '/api/projects/p1/view/files/sheets/power%20%231.kicad_sch',
    );
  });
});

describe('agentViewUrl', () => {
  it('builds a pcb URL with comma-separated layers in canonical order', () => {
    expect(agentViewUrl({ projectId: 'p1', doc: 'pcb', theme: 'dark', layers: ['B.Cu', 'F.Cu', 'Edge.Cuts'], rev: 7 })).toBe(
      '/api/projects/p1/agent-view?doc=pcb&theme=dark&layers=F.Cu,B.Cu,Edge.Cuts&v=7',
    );
  });
  it('drops layers for the schematic', () => {
    expect(agentViewUrl({ projectId: 'p1', doc: 'sch', theme: 'light', layers: ['F.Cu'], rev: null })).toBe(
      '/api/projects/p1/agent-view?doc=sch&theme=light',
    );
  });
  it('passes mode=kicad through', () => {
    expect(agentViewUrl({ projectId: 'p1', doc: 'sch', theme: 'light', layers: [], rev: 2, mode: 'kicad' })).toBe(
      '/api/projects/p1/agent-view?doc=sch&theme=light&mode=kicad&v=2',
    );
  });
  it('has eight layers and sensible defaults', () => {
    expect(PCB_LAYERS).toHaveLength(8);
    expect(DEFAULT_LAYERS).toEqual(['F.Cu', 'B.Cu', 'F.SilkS', 'B.SilkS', 'Edge.Cuts']);
  });
});
