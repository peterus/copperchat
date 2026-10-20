import { useCallback, useState } from 'react';
import { approveDoc, clearUserEdited, type DocRead, docErrorCode, unapproveDoc } from '@/api/docs';
import { Markdown } from '@/components/chat/Markdown';
import { errorText } from '@/components/history/restore';
import { useUi } from '@/stores/ui';
import { BADGE_CLASS, BUTTON, docBadge, isDecisions, PRIMARY, partOfLink } from './format';

export interface DocViewProps {
  projectId: string;
  doc: DocRead;
  onEdit: () => void;
  onHistory: () => void;
  /** Set after the user saved a doc that was approved: the version to approve again. */
  reapproveVersion: string | null;
  onReapproveDone: () => void;
  /** Reload the doc and the index after a state change. */
  onChanged: () => void;
  /** An unsaved editor draft of this doc exists (resumed by [Edit]). */
  hasDraft?: boolean;
  onDiscardDraft?: () => void;
}

/** A doc rendered with the chat `Markdown` (sanitised), plus its status controls (spec §12.2). */
export function DocView({
  projectId,
  doc,
  onEdit,
  onHistory,
  reapproveVersion,
  onReapproveDone,
  onChanged,
  hasDraft = false,
  onDiscardDraft,
}: DocViewProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const decisions = isDecisions(doc.path);
  const badge = docBadge({ ...doc.meta, exists: doc.exists });

  // `../parts/<MPN>/notes.md` opens the Parts panel at that part; other relative links are text.
  const onRelativeLink = useCallback((href: string) => {
    const mpn = partOfLink(href);
    return mpn ? () => useUi.getState().openPanel('parts', { mpn }) : null;
  }, []);

  async function act(call: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    setStale(false);
    try {
      await call();
      onChanged();
      return true;
    } catch (e) {
      if (docErrorCode(e) === 'doc_conflict') setStale(true);
      else setError(errorText(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  const version = doc.version;
  return (
    <div className="flex flex-col gap-3 p-3">
      <div className="flex flex-wrap items-center gap-2">
        {!decisions && <span className={`rounded border px-1.5 text-xs ${BADGE_CLASS[badge]}`}>{badge}</span>}
        {doc.meta.userEdited && <span className="rounded bg-surface-2 px-1.5 text-xs text-muted">edited by you</span>}
        <span className="mr-auto" />
        <button type="button" className={BUTTON} disabled={busy} onClick={onEdit}>
          {hasDraft ? 'Continue editing' : doc.exists ? 'Edit' : 'Create'}
        </button>
        {!decisions && version && !doc.meta.approved && (
          <button type="button" className={PRIMARY} disabled={busy} onClick={() => void act(() => approveDoc(projectId, doc.path, version))}>
            Approve
          </button>
        )}
        {!decisions && version && doc.meta.approved && (
          <button type="button" className={BUTTON} disabled={busy} onClick={() => void act(() => unapproveDoc(projectId, doc.path, version))}>
            Unapprove
          </button>
        )}
        <button type="button" className={BUTTON} onClick={onHistory}>
          History
        </button>
      </div>
      {hasDraft && (
        <p role="status" className="flex flex-wrap items-center gap-2 rounded border border-warning px-2 py-1 text-sm">
          <span className="mr-auto">You have unsaved changes to this doc.</span>
          <button
            type="button"
            className={BUTTON}
            onClick={() => window.confirm('Discard your unsaved changes to this doc?') && onDiscardDraft?.()}
          >
            Discard draft
          </button>
        </p>
      )}
      {doc.meta.userEdited && version && (
        <p className="flex flex-wrap items-center gap-2 text-xs text-muted">
          You edited this doc: the agent only patches sections or appends.
          <button type="button" className={BUTTON} disabled={busy} onClick={() => void act(() => clearUserEdited(projectId, doc.path, version))}>
            Let the agent rewrite
          </button>
        </p>
      )}
      {reapproveVersion && !decisions && (
        <div role="status" className="flex flex-wrap items-center gap-2 rounded border border-warning px-2 py-1 text-sm">
          <span className="mr-auto">Saved. This doc was approved — approve again?</span>
          <button
            type="button"
            className={PRIMARY}
            disabled={busy}
            onClick={() =>
              void act(() => approveDoc(projectId, doc.path, reapproveVersion)).then((ok) => ok && onReapproveDone())
            }
          >
            Approve again
          </button>
          <button type="button" className={BUTTON} onClick={onReapproveDone}>
            Not now
          </button>
        </div>
      )}
      {stale && (
        <p role="alert" className="flex items-center gap-2 rounded border border-warning px-2 py-1 text-sm">
          <span className="mr-auto">This doc changed — reload and check it before approving.</span>
          <button
            type="button"
            className={BUTTON}
            onClick={() => {
              setStale(false);
              onReapproveDone();
              onChanged();
            }}
          >
            Reload
          </button>
        </p>
      )}
      {error && (
        <p role="alert" className="rounded border border-danger bg-danger/10 px-2 py-1 text-sm text-danger">
          {error}
        </p>
      )}
      {doc.exists ? (
        <article className="text-sm">
          <Markdown text={doc.body} onRelativeLink={onRelativeLink} />
        </article>
      ) : (
        <div className="text-sm">
          <p className="text-muted">This doc does not exist yet. The agent or you start it from this template:</p>
          {doc.template && (
            <article className="mt-2 rounded border border-dashed border-border p-2 opacity-75">
              <Markdown text={doc.template} onRelativeLink={onRelativeLink} />
            </article>
          )}
        </div>
      )}
    </div>
  );
}
