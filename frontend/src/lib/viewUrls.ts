import { apiUrl } from '@/api/http';
import type { ResolvedTheme } from '@/stores/ui';

export type DocKind = 'sch' | 'pcb';
export type ViewMode = 'kicad' | 'agent';

export const PCB_LAYERS = ['F.Cu', 'B.Cu', 'F.SilkS', 'B.SilkS', 'Edge.Cuts', 'F.Fab', 'B.Fab', 'User.Drawings'] as const;
export type PcbLayer = (typeof PCB_LAYERS)[number];
export const DEFAULT_LAYERS: PcbLayer[] = ['F.Cu', 'B.Cu', 'F.SilkS', 'B.SilkS', 'Edge.Cuts'];

export function canonicalLayers(layers: readonly PcbLayer[]): PcbLayer[] {
  return PCB_LAYERS.filter((l) => layers.includes(l));
}

const project = (id: string) => `/projects/${encodeURIComponent(id)}`;

/**
 * A KiCanvas source file from the open response's `viewFiles` (index X3). Sub-sheets may live in
 * sub-directories, so every path segment is encoded on its own. `rev` is the viewer's reload
 * counter (null before the first `view-updated`: the server revalidates via ETag).
 */
export function viewFileUrl(projectId: string, name: string, rev: number | null): string {
  const path = name.split('/').map(encodeURIComponent).join('/');
  const base = apiUrl(`${project(projectId)}/view/files/${path}`);
  return rev === null ? base : `${base}?v=${rev}`;
}

export function agentViewUrl(o: {
  projectId: string;
  doc: DocKind;
  theme: ResolvedTheme;
  layers: readonly PcbLayer[];
  rev: number | null;
  mode?: 'agent' | 'kicad';
}): string {
  const q = [`doc=${o.doc}`, `theme=${o.theme}`];
  if (o.doc === 'pcb' && o.mode !== 'kicad') {
    q.push(`layers=${canonicalLayers(o.layers).map(encodeURIComponent).join(',')}`);
  }
  if (o.mode === 'kicad') q.push('mode=kicad');
  if (o.rev !== null) q.push(`v=${o.rev}`);
  return `${apiUrl(`${project(o.projectId)}/agent-view`)}?${q.join('&')}`;
}
