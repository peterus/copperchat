import { useEffect, useState } from 'react';
import type { DocConflict } from '@/api/docs';
import { DocEditor } from '@/components/docs/DocEditor';
import { DocHistory } from '@/components/docs/DocHistory';
import { DocIndex } from '@/components/docs/DocIndex';
import { DocView } from '@/components/docs/DocView';
import { docName } from '@/components/docs/format';
import type { PanelProps } from '@/layouts/panels';
import { useDocs, useDocsStore } from '@/stores/docs';

type Nav =
  | { view: 'index' }
  | { view: 'doc'; path: string }
  | { view: 'edit'; path: string; seed?: { draft: string; conflict: DocConflict } }
  | { view: 'history'; path: string; revisionId: number | null };

/** `{path}` opens that doc, `{path, revisionId}` its diff in the history, nothing the index. */
function navOf(path: string | undefined, revisionId: string | undefined): Nav {
  if (!path) return { view: 'index' };
  const rid = revisionId !== undefined && /^[1-9]\d{0,15}$/.test(revisionId) ? Number(revisionId) : null;
  return rid === null ? { view: 'doc', path } : { view: 'history', path, revisionId: rid };
}

/**
 * The Docs panel (spec §12.2): index → doc → history, edit. One column at every width; on the
 * phone it fills the More tab and the editor the whole viewport. Remounted per project.
 */
export function DocsPanel({ projectId, params }: PanelProps) {
  return <DocsPanelBody key={projectId} projectId={projectId} path={params?.path} revisionId={params?.revisionId} />;
}

function DocsPanelBody({
  projectId,
  path: paramPath,
  revisionId: paramRevision,
}: {
  projectId: string;
  path: string | undefined;
  revisionId: string | undefined;
}) {
  const store = useDocsStore();
  const [nav, setNav] = useState<Nav>(() => navOf(paramPath, paramRevision));
  const [reapprove, setReapprove] = useState<{ path: string; version: string } | null>(null);
  const index = useDocs((s) => s.index);
  const indexError = useDocs((s) => s.indexError);
  const doc = useDocs((s) => s.doc);
  const docError = useDocs((s) => s.docError);

  useEffect(() => {
    void store.getState().load(projectId);
  }, [store, projectId]);

  // A later openPanel('docs', …) while the panel is open (e.g. another [View diff]).
  useEffect(() => {
    setNav(navOf(paramPath, paramRevision));
  }, [paramPath, paramRevision]);

  const openPath = nav.view === 'index' ? null : nav.path;
  useEffect(() => {
    void store.getState().open(openPath);
  }, [store, openPath]);

  const reload = () => {
    void store.getState().reloadDoc();
    void store.getState().refreshIndex();
  };
  const loaded = doc !== null && doc.path === openPath ? doc : null;

  let back: { label: string; to: Nav } | null = null;
  if (nav.view === 'doc') back = { label: 'Docs', to: { view: 'index' } };
  if (nav.view === 'history') back = { label: docName(nav.path), to: { view: 'doc', path: nav.path } };

  let body;
  if (nav.view === 'index') {
    body = index ? (
      <DocIndex index={index} onOpen={(path) => setNav({ view: 'doc', path })} />
    ) : (
      !indexError && <p className="p-3 text-sm text-muted">Loading…</p>
    );
  } else if (!loaded) {
    body = !docError && <p className="p-3 text-sm text-muted">Loading…</p>;
  } else if (nav.view === 'doc') {
    body = (
      <DocView
        projectId={projectId}
        doc={loaded}
        onEdit={() => setNav({ view: 'edit', path: nav.path })}
        onHistory={() => setNav({ view: 'history', path: nav.path, revisionId: null })}
        reapproveVersion={reapprove?.path === nav.path ? reapprove.version : null}
        onReapproveDone={() => setReapprove(null)}
        onChanged={reload}
      />
    );
  } else if (nav.view === 'edit') {
    body = (
      <DocEditor
        projectId={projectId}
        doc={loaded}
        seed={nav.seed}
        onCancel={() => {
          setNav({ view: 'doc', path: nav.path });
          reload();
        }}
        onSaved={(version, wasApproved) => {
          setReapprove(wasApproved ? { path: nav.path, version } : null);
          setNav({ view: 'doc', path: nav.path });
          reload();
        }}
      />
    );
  } else {
    body = (
      <DocHistory
        projectId={projectId}
        path={nav.path}
        currentVersion={loaded.exists ? loaded.version : null}
        selected={nav.revisionId}
        onSelect={(revisionId) => setNav({ view: 'history', path: nav.path, revisionId })}
        onReverted={reload}
        onRevertConflict={(draft, conflict) => setNav({ view: 'edit', path: nav.path, seed: { draft, conflict } })}
      />
    );
  }

  const error = nav.view === 'index' ? indexError : docError;
  return (
    <div className="flex h-full flex-col">
      {(back || nav.view !== 'index') && nav.view !== 'edit' && (
        <div className="flex items-center gap-2 border-b border-border px-3 py-1.5">
          {back && (
            <button type="button" className="text-sm text-accent" onClick={() => setNav(back.to)}>
              ← {back.label}
            </button>
          )}
          {nav.view === 'doc' && <span className="truncate font-mono text-sm">{docName(nav.path)}</span>}
          {nav.view === 'history' && <span className="text-sm text-muted">History</span>}
        </div>
      )}
      {error && (
        <p role="alert" className="m-3 rounded border border-danger bg-danger/10 px-2 py-1 text-sm text-danger">
          {error}
        </p>
      )}
      <div className="min-h-0 flex-1 overflow-auto">{body}</div>
    </div>
  );
}
