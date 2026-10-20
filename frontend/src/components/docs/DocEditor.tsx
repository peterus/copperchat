import { type KeyboardEvent, useEffect, useState } from 'react';
import { type DocConflict, docConflictOf, type DocRead, getDoc, putDoc } from '@/api/docs';
import { Markdown } from '@/components/chat/Markdown';
import { errorText } from '@/components/history/restore';
import { useLayoutMode } from '@/layouts/ProjectLayout';
import { draftKey, useDocs, useDocsStore } from '@/stores/docs';
import { BUTTON, conflictBy, docName, PRIMARY } from './format';
import { LineDiff } from './LineDiff';

/** What the user's text is written on top of: a save sends `version` as `baseVersion`. */
interface Base {
  version: string | null;
  body: string;
  approved: boolean;
}

export interface DocEditorProps {
  projectId: string;
  /** The doc as loaded when editing started; later reloads never replace the text. */
  doc: DocRead;
  /** A revert that hit a 409: its text and the conflict, so the same three choices apply. */
  seed?: { draft: string; conflict: DocConflict } | undefined;
  onCancel: () => void;
  /** `wasApproved`: the doc was approved at the base version, so the panel offers to approve again. */
  onSaved: (version: string, wasApproved: boolean) => void;
}

/** In the preview, relative links are plain text. */
const noLinks = () => null;

const baseOf = (d: DocRead): Base => ({
  version: d.exists ? d.version : null,
  body: d.exists ? d.body : '',
  approved: d.meta.approved,
});

/**
 * Markdown textarea with a preview (spec §12.2). The front matter is never shown: it is
 * server-owned. A save never overwrites silently: a 409 offers [Show their change], [Keep
 * editing on top of current] and [Discard mine]; the draft is only dropped by the last one.
 */
export function DocEditor({ projectId, doc, seed, onCancel, onSaved }: DocEditorProps) {
  const store = useDocsStore();
  // An unsaved draft of this doc (the editor was closed by the sheet, a panel switch, …) resumes.
  const [saved] = useState(() => (seed ? undefined : store.getState().drafts[draftKey(projectId, doc.path)]));
  const [draft, setDraft] = useState(() => seed?.draft ?? saved?.text ?? (doc.exists ? doc.body : (doc.template ?? '')));
  const [base, setBase] = useState<Base>(() => saved?.base ?? baseOf(doc));
  const [preview, setPreview] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<DocConflict | null>(seed?.conflict ?? saved?.conflict ?? null);
  /** The current doc as shown by [Show their change]: [Keep editing] re-bases onto exactly this. */
  const [theirs, setTheirs] = useState<DocRead | null>(null);
  // The template counts as unchanged only for the base the editor started from (not after a
  // re-base onto a since-deleted doc).
  const [initialBase] = useState(base);
  const pristine = draft === base.body || (base === initialBase && base.version === null && draft === (doc.template ?? ''));
  const dirty = !pristine || conflict !== null;

  // Every change is mirrored to the store, so no unmount can lose it.
  useEffect(() => {
    store.getState().setDraft(projectId, doc.path, dirty ? { text: draft, base, conflict } : null);
  }, [store, projectId, doc.path, dirty, draft, base, conflict]);

  const leave = (then: () => void) => {
    store.getState().setDraft(projectId, doc.path, null);
    then();
  };
  const cancel = () => {
    if (dirty && !window.confirm('Discard your unsaved changes to this doc?')) return;
    leave(onCancel);
  };
  // Escape must not reach the panel sheet's close handler (it would unmount the editor).
  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === 'Escape') e.stopPropagation();
  };
  const live = useDocs((s) => s.live[doc.path]);
  const changedMeanwhile = live !== undefined && live.version !== base.version && conflict === null;
  const fullscreen = useLayoutMode() === 'phone';

  async function save() {
    setBusy(true);
    setError(null);
    try {
      const version = await putDoc(projectId, doc.path, draft, base.version);
      leave(() => onSaved(version, base.approved));
    } catch (e) {
      const c = docConflictOf(e);
      if (c) {
        setConflict(c);
        setTheirs(null);
      } else setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function current(): Promise<DocRead | null> {
    try {
      return await getDoc(projectId, doc.path);
    } catch (e) {
      setError(errorText(e));
      return null;
    }
  }

  async function showTheirs() {
    setBusy(true);
    const cur = await current();
    setBusy(false);
    if (cur) setTheirs(cur);
  }

  async function rebase() {
    const ok = window.confirm(
      'Keep your text and save it on top of the current version? Their change is replaced by your text where they differ.',
    );
    if (!ok) return;
    let cur = theirs;
    if (!cur) {
      setBusy(true);
      cur = await current();
      setBusy(false);
    }
    if (!cur) return;
    setBase(baseOf(cur));
    setConflict(null);
    setTheirs(null);
  }

  return (
    <div
      onKeyDown={onKeyDown}
      data-fullscreen={fullscreen ? 'true' : undefined}
      className={
        fullscreen
          ? 'fixed inset-0 z-50 flex flex-col gap-2 bg-bg p-2 pb-[max(0.5rem,env(safe-area-inset-bottom))] pt-[max(0.5rem,env(safe-area-inset-top))]'
          : 'flex h-full flex-col gap-2 p-3'
      }
    >
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="mr-auto truncate font-mono text-sm font-medium">{docName(doc.path)}</h3>
        <button type="button" className={BUTTON} aria-pressed={preview} onClick={() => setPreview((p) => !p)}>
          {preview ? 'Edit text' : 'Preview'}
        </button>
        <button type="button" className={PRIMARY} disabled={busy || conflict !== null} onClick={() => void save()}>
          Save
        </button>
        <button type="button" className={BUTTON} disabled={busy} onClick={cancel}>
          Cancel
        </button>
      </div>
      {changedMeanwhile && (
        <p role="status" className="rounded border border-warning px-2 py-1 text-sm">
          This doc changed — your save will conflict.
        </p>
      )}
      {conflict && (
        <div role="alert" className="flex flex-col gap-2 rounded border border-warning px-2 py-1 text-sm">
          <p>Changed while you were editing (by {conflictBy(conflict)}).</p>
          <div className="flex flex-wrap gap-2">
            <button type="button" className={BUTTON} disabled={busy} onClick={() => void showTheirs()}>
              Show their change
            </button>
            <button type="button" className={BUTTON} disabled={busy} onClick={() => void rebase()}>
              Keep editing on top of current
            </button>
            <button type="button" className={BUTTON} disabled={busy} onClick={() => leave(onCancel)}>
              Discard mine
            </button>
          </div>
          {theirs !== null && (
            <section aria-label="Their change" className="max-h-80 overflow-auto">
              <LineDiff before={base.body} after={theirs.exists ? theirs.body : ''} />
            </section>
          )}
        </div>
      )}
      {error && (
        <p role="alert" className="rounded border border-danger bg-danger/10 px-2 py-1 text-sm text-danger">
          {error}
        </p>
      )}
      {preview ? (
        <article className="min-h-0 flex-1 overflow-auto text-sm">
          <Markdown text={draft} onRelativeLink={noLinks} />
        </article>
      ) : (
        <textarea
          aria-label="Doc text"
          className="min-h-[50vh] w-full flex-1 rounded border border-border bg-surface p-2 font-mono text-xs"
          value={draft}
          // Read-only while saving: the save sends the text of the click, and success closes the editor.
          readOnly={busy}
          onChange={(e) => setDraft(e.target.value)}
        />
      )}
    </div>
  );
}
