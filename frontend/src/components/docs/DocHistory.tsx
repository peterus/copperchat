import { useCallback, useEffect, useRef, useState } from 'react';
import {
  type DocConflict,
  docConflictOf,
  getRevision,
  listRevisions,
  revertDoc,
  type RevisionContent,
  type RevisionDTO,
} from '@/api/docs';
import { errorText } from '@/components/history/restore';
import { useDocs } from '@/stores/docs';
import { BUTTON, changedBy, docBody } from './format';
import { LineDiff } from './LineDiff';

export interface DocHistoryProps {
  projectId: string;
  path: string;
  /** The doc's current version: a revert is a user write on top of it. */
  currentVersion: string | null;
  /** The revision to show (null: the list). */
  selected: number | null;
  onSelect: (revisionId: number | null) => void;
  onReverted: () => void;
  /** A revert hit a 409: continue in the editor with the revision's text. */
  onRevertConflict: (draft: string, conflict: DocConflict) => void;
}

const when = (iso: string) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
};

/** Revisions newest first; a revision shows the diff against its predecessor (spec §7.6). */
export function DocHistory({ projectId, path, currentVersion, selected, onSelect, onReverted, onRevertConflict }: DocHistoryProps) {
  const [revisions, setRevisions] = useState<RevisionDTO[] | null>(null);
  const [content, setContent] = useState<{ id: number; data: RevisionContent } | null>(null);
  // The list's own error, separate from a revision/revert error: one never clears the other.
  const [listError, setListError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const liveVersion = useDocs((s) => s.live[path]?.version);
  const token = useRef(0);
  const listToken = useRef(0);
  const shownId = useRef<number | null>(null);

  // One request at a time per doc version; a slower, older response never replaces a newer list.
  const loadList = useCallback(async () => {
    const t = ++listToken.current;
    try {
      const list = await listRevisions(projectId, path);
      if (t === listToken.current) {
        setRevisions([...list].sort((a, b) => b.id - a.id));
        setListError(null);
      }
    } catch (e) {
      if (t === listToken.current) setListError(errorText(e));
    }
  }, [projectId, path]);

  useEffect(() => {
    void loadList();
  }, [loadList, liveVersion]);

  useEffect(() => {
    const t = ++token.current;
    setError(null);
    if (selected === null) return;
    if (shownId.current === selected) return; // revisions are immutable: no refetch
    getRevision(projectId, path, selected).then(
      (data) => {
        if (t !== token.current) return;
        shownId.current = selected;
        setContent({ id: selected, data });
      },
      (e: unknown) => t === token.current && setError(errorText(e)),
    );
  }, [projectId, path, selected]);

  async function revert(rid: number, data: RevisionContent) {
    setBusy(true);
    setError(null);
    try {
      await revertDoc(projectId, path, rid, currentVersion);
      onSelect(null);
      onReverted();
    } catch (e) {
      const c = docConflictOf(e);
      if (c) onRevertConflict(docBody(data.content), c);
      else setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const errorRow = (error || listError) && (
    <p role="alert" className="rounded border border-danger bg-danger/10 px-2 py-1 text-sm text-danger">
      {error ?? listError}
    </p>
  );

  if (selected !== null) {
    const rev = revisions?.find((r) => r.id === selected);
    const shown = content?.id === selected ? content.data : null;
    return (
      <div className="flex flex-col gap-2 p-3">
        <button type="button" className="self-start text-sm text-accent" onClick={() => onSelect(null)}>
          ← All revisions
        </button>
        {rev && (
          <p className="text-xs text-muted">
            {when(rev.createdAt)} · {changedBy(rev.author, rev.conversationTitle)}
            {rev.summary && ` · ${rev.summary}`}
          </p>
        )}
        {errorRow}
        {shown && (
          <>
            <LineDiff before={docBody(shown.prevContent ?? '')} after={docBody(shown.content)} />
            <button
              type="button"
              className={`${BUTTON} self-start`}
              disabled={busy}
              onClick={() => void revert(selected, shown)}
            >
              Revert to this
            </button>
          </>
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2 p-3">
      {errorRow}
      {revisions?.length === 0 && <p className="text-sm text-muted">No revisions yet.</p>}
      <ul aria-label="Revisions" className="flex flex-col gap-1">
        {revisions?.map((r) => (
          <li key={r.id}>
            <button
              type="button"
              onClick={() => onSelect(r.id)}
              className="flex w-full flex-col rounded px-2 py-1.5 text-left hover:bg-surface"
            >
              <span className="flex flex-wrap items-center gap-2 text-sm">
                <span>{changedBy(r.author, r.conversationTitle)}</span>
                <span className="text-success">+{r.added}</span>
                <span className="text-danger">−{r.removed}</span>
              </span>
              <span className="text-xs text-muted">
                {when(r.createdAt)}
                {r.summary && ` · ${r.summary}`}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
