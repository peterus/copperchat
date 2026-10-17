import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { PoweredBy } from '@/components/PoweredBy';
import { useProjects } from '@/stores/projects';

export function ProjectPickerPage() {
  const projects = useProjects((s) => s.projects);
  const loading = useProjects((s) => s.loading);
  const error = useProjects((s) => s.error);
  const load = useProjects((s) => s.load);
  const [filter, setFilter] = useState('');

  useEffect(() => {
    void load();
  }, [load]);

  const shown = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return f ? projects.filter((p) => `${p.name} ${p.relPath}`.toLowerCase().includes(f)) : projects;
  }, [projects, filter]);

  return (
    <main className="mx-auto flex min-h-full max-w-3xl flex-col gap-4 p-4">
      <header className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Projects</h1>
        <Link to="/settings" className="text-sm text-accent">
          Settings
        </Link>
      </header>
      <input
        type="search"
        aria-label="Filter projects"
        placeholder="Filter…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        className="rounded border border-border bg-surface px-3 py-2"
      />
      {loading && projects.length === 0 && <p className="text-muted">Loading…</p>}
      {error && (
        <div role="alert" className="flex items-center gap-3 text-danger">
          <span className="flex-1">Could not load projects: {error}</span>
          <button type="button" className="rounded border border-border px-2 py-1 text-sm" onClick={() => void load()}>
            Retry
          </button>
        </div>
      )}
      {!loading && !error && projects.length === 0 && (
        <p className="text-muted">No KiCad projects found under the projects directory.</p>
      )}
      <ul className="space-y-2">
        {shown.map((p) => (
          <li key={p.id}>
            <Link
              to={`/p/${p.id}`}
              className="block rounded-lg border border-border bg-surface px-4 py-3 hover:border-accent"
            >
              <span className="font-medium">{p.name}</span>
              <span className="block text-xs text-muted">{p.relPath}</span>
              <span className="mt-1 flex gap-2 text-xs">
                {p.hasSch && <span className="rounded bg-surface-2 px-1.5">Schematic</span>}
                {p.hasPcb && <span className="rounded bg-surface-2 px-1.5">PCB</span>}
              </span>
              {p.locked && (
                <span className="mt-2 block text-xs text-warning">
                  Open in KiCad — close it there. CopperChat will open it read-only.
                </span>
              )}
            </Link>
          </li>
        ))}
      </ul>
      <footer className="mt-auto">
        <PoweredBy />
      </footer>
    </main>
  );
}
