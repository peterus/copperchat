import { ChevronRight, ListTree } from 'lucide-react';
import { useState } from 'react';
import { breadcrumb, countSheets, type SheetNode } from '@/lib/kicanvas';
import { sameViewFile } from '@/lib/viewUrls';

interface Props {
  tree: SheetNode[];
  active: string | null;
  /** View files changed since they were loaded ("Live view"): their sheets get a dot. */
  stale?: readonly string[];
  onOpen: (id: string) => void;
  /** Close the tree after picking a sheet (touch devices: it covers the drawing). */
  closeOnPick: boolean;
}

/** Breadcrumb ("Root › Power › Regulator") plus a toggleable, indented sheet tree. */
export function SheetNavigator({ tree, active, stale = [], onOpen, closeOnPick }: Props) {
  const [open, setOpen] = useState(false);
  const crumbs = breadcrumb(tree, active);
  const isStale = (n: SheetNode) => stale.some((f) => sameViewFile(f, n.filename));
  const anyStale = stale.length > 0 && hasStale(tree, isStale);
  const pick = (id: string) => {
    onOpen(id);
    if (closeOnPick) setOpen(false);
  };

  return (
    <div className="pointer-events-none absolute inset-x-2 top-2 z-10 flex flex-col items-start gap-1">
      <div className="pointer-events-auto flex max-w-full items-center gap-1 rounded border border-border bg-bg/90 px-1 py-0.5 text-sm shadow backdrop-blur">
        <button
          type="button"
          aria-expanded={open}
          aria-label={`Sheets (${countSheets(tree)}${anyStale ? ', updated' : ''})`}
          className="relative flex h-8 shrink-0 items-center gap-1 rounded px-1.5 text-muted hover:text-fg"
          onClick={() => setOpen((o) => !o)}
        >
          <ListTree size={16} />
          {anyStale && <StaleDot className="absolute top-1 right-0.5" />}
        </button>
        <nav aria-label="Sheet path" className="min-w-0 overflow-x-auto">
          <ol className="flex items-center whitespace-nowrap">
            {crumbs.map((n, i) => (
              <li key={n.id} className="flex items-center">
                {i > 0 && <ChevronRight size={14} className="mx-0.5 shrink-0 text-muted" aria-hidden />}
                {i === crumbs.length - 1 ? (
                  <span aria-current="page" className="px-1 font-medium">
                    {n.name}
                  </span>
                ) : (
                  <button type="button" className="rounded px-1 py-1 text-accent hover:underline" onClick={() => onOpen(n.id)}>
                    {n.name}
                  </button>
                )}
              </li>
            ))}
          </ol>
        </nav>
      </div>
      {open && (
        <nav
          aria-label="Sheets"
          className="pointer-events-auto max-h-[60vh] w-64 max-w-full overflow-y-auto rounded border border-border bg-bg/95 p-1 text-sm shadow backdrop-blur"
        >
          <SheetList nodes={tree} depth={0} active={active} isStale={isStale} onPick={pick} />
        </nav>
      )}
    </div>
  );
}

const hasStale = (nodes: SheetNode[], isStale: (n: SheetNode) => boolean): boolean =>
  nodes.some((n) => isStale(n) || hasStale(n.children, isStale));

/** A sheet changed since it was loaded; opening it loads the new version. */
const StaleDot = ({ className = '' }: { className?: string }) => (
  <span aria-hidden className={`h-1.5 w-1.5 shrink-0 rounded-full bg-warning ${className}`} />
);

function SheetList({
  nodes,
  depth,
  active,
  isStale,
  onPick,
}: {
  nodes: SheetNode[];
  depth: number;
  active: string | null;
  isStale: (n: SheetNode) => boolean;
  onPick: (id: string) => void;
}) {
  return (
    <ul role={depth === 0 ? 'tree' : 'group'} aria-label={depth === 0 ? 'Sheet hierarchy' : undefined}>
      {nodes.map((n) => (
        <li key={n.id} role="treeitem" aria-selected={n.id === active} aria-expanded={n.children.length ? true : undefined}>
          <button
            type="button"
            style={{ paddingLeft: `${0.5 + depth * 1}rem` }}
            className={`flex min-h-9 w-full items-center rounded pr-2 text-left ${
              n.id === active ? 'bg-accent text-accent-fg' : 'hover:bg-surface-2'
            }`}
            title={isStale(n) ? `${n.filename} (changed, opening it loads the new version)` : n.filename}
            onClick={() => onPick(n.id)}
          >
            <span className="min-w-0 flex-1">{n.name}</span>
            {isStale(n) && (
              <>
                <StaleDot className="ml-2" />
                <span className="sr-only">, updated</span>
              </>
            )}
          </button>
          {n.children.length > 0 && (
            <SheetList nodes={n.children} depth={depth + 1} active={active} isStale={isStale} onPick={onPick} />
          )}
        </li>
      ))}
    </ul>
  );
}
