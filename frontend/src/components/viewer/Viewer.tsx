import { Maximize2, Minimize2, Search } from 'lucide-react';
import { type FormEvent, useEffect, useState } from 'react';
import type { DocKind } from '@/lib/viewUrls';
import { useProjects } from '@/stores/projects';
import { useUi } from '@/stores/ui';
import { useView } from '@/stores/view';
import { AgentViewPane } from './AgentViewPane';
import { KicadDocView } from './KicadDocView';
import { LayerToggles } from './LayerToggles';
import { type FindRequest, insertMention } from './types';

const TABS: Array<{ doc: DocKind; label: string }> = [
  { doc: 'sch', label: 'Schematic' },
  { doc: 'pcb', label: 'PCB' },
];
const NO_FILES = { sch: [] as string[], pcb: [] as string[] };

const seg = (on: boolean) => `rounded px-2 py-1 text-sm ${on ? 'bg-accent text-accent-fg' : 'text-muted hover:text-fg'}`;

export function Viewer({ projectId }: { projectId: string }) {
  const theme = useUi((s) => s.resolvedTheme);
  const v = useView((s) => s);
  const { setProject, setFullscreen, fullscreen } = v;
  const current = useProjects((s) => (s.current?.id === projectId ? s.current : null));
  const files = current?.viewFiles ?? NO_FILES;
  const bytes = current?.viewBytes ?? {};

  // Every open (a new KiCad session) restarts the server's view counter (X2).
  useEffect(() => setProject(projectId), [projectId, current, setProject]);
  useEffect(() => {
    if (!fullscreen) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setFullscreen(false);
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [fullscreen, setFullscreen]);

  // KiCad panes stay mounted once visited (also in agent mode): switching back does not re-parse.
  const [visited, setVisited] = useState<Set<DocKind>>(() => new Set([v.doc]));
  useEffect(() => setVisited((s) => (s.has(v.doc) ? s : new Set(s).add(v.doc))), [v.doc]);

  const [query, setQuery] = useState('');
  const [find, setFind] = useState<FindRequest | null>(null);
  const [findResult, setFindResult] = useState<string | null>(null);
  const submitFind = (e: FormEvent) => {
    e.preventDefault();
    const ref = query.trim();
    if (!ref) return;
    v.setMode('kicad');
    setFindResult(null);
    setFind((f) => ({ ref, seq: (f?.seq ?? 0) + 1 }));
  };
  const onFindResult = (ref: string, found: boolean) =>
    setFindResult(found ? null : `${ref} not found in the ${v.doc === 'sch' ? 'schematic' : 'board'}.`);

  return (
    <section
      aria-label="Viewer"
      className={`flex flex-col bg-bg ${fullscreen ? 'fixed inset-0 z-50 pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)]' : 'h-full'}`}
    >
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-2 py-1">
        <div role="tablist" aria-label="Document" className="flex gap-1">
          {TABS.map((t) => (
            <button
              key={t.doc}
              type="button"
              role="tab"
              aria-selected={v.doc === t.doc}
              className={seg(v.doc === t.doc)}
              onClick={() => {
                v.setDoc(t.doc);
                setFind(null);
                setFindResult(null);
              }}
            >
              {t.label}
            </button>
          ))}
        </div>
        <div className="flex gap-1" role="group" aria-label="View mode">
          <button type="button" aria-pressed={v.mode === 'kicad'} className={seg(v.mode === 'kicad')} onClick={() => v.setMode('kicad')}>
            KiCad view
          </button>
          <button type="button" aria-pressed={v.mode === 'agent'} className={seg(v.mode === 'agent')} onClick={() => v.setMode('agent')}>
            Agent view
          </button>
        </div>
        <form role="search" onSubmit={submitFind} className="flex items-center gap-1">
          <input
            aria-label="Find reference"
            placeholder="Find REF"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="w-24 rounded border border-border bg-bg px-2 py-0.5 text-sm"
            autoCapitalize="characters"
            autoCorrect="off"
            spellCheck={false}
          />
          <button type="submit" aria-label="Find" className="rounded p-1 text-muted hover:text-fg">
            <Search size={16} />
          </button>
        </form>
        <button
          type="button"
          className="ml-auto rounded p-1 text-muted hover:text-fg"
          aria-label={fullscreen ? 'Exit fullscreen' : 'Fullscreen'}
          onClick={() => setFullscreen(!fullscreen)}
        >
          {fullscreen ? <Minimize2 size={18} /> : <Maximize2 size={18} />}
        </button>
        {findResult && (
          <p role="status" className="w-full text-xs text-warning">
            {findResult}
          </p>
        )}
        {v.mode === 'agent' && v.doc === 'pcb' && (
          <div className="w-full">
            <LayerToggles layers={v.layers} onToggle={v.toggleLayer} />
          </div>
        )}
      </div>
      <div className="relative min-h-0 flex-1">
        {TABS.filter((t) => visited.has(t.doc)).map((t) => (
          <div key={t.doc} className="absolute inset-0" hidden={t.doc !== v.doc || v.mode !== 'kicad'}>
            <KicadDocView
              projectId={projectId}
              doc={t.doc}
              files={files[t.doc]}
              bytes={files[t.doc].reduce((sum, f) => sum + (bytes[f] ?? 0), 0)}
              layers={v.layers}
              rev={v.revs[t.doc]}
              theme={theme}
              onPick={insertMention}
              find={t.doc === v.doc ? find : null}
              onFindResult={onFindResult}
            />
          </div>
        ))}
        {v.mode === 'agent' && (
          <div className="absolute inset-0">
            <AgentViewPane projectId={projectId} doc={v.doc} rev={v.revs[v.doc]} theme={theme} layers={v.layers} />
          </div>
        )}
      </div>
    </section>
  );
}
