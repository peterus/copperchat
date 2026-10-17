import { useCallback, useEffect, useRef, useState } from 'react';
import { datasheetUrl, getNotes, listParts, saveNotes, stripFrontMatter, type PartInfo, type PartNotes } from '@/api/parts';
import { Markdown } from '@/components/chat/Markdown';
import type { PanelProps } from '@/layouts/panels';

const BUTTON = 'rounded border border-border px-2 py-1 text-xs hover:bg-surface-2 disabled:opacity-50';
const PRIMARY = 'rounded bg-accent px-2 py-1 text-xs font-medium text-accent-fg disabled:opacity-50';

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function PartsPanel({ projectId }: PanelProps) {
  const [parts, setParts] = useState<PartInfo[] | null>(null);
  const [notes, setNotes] = useState<PartNotes | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const openToken = useRef(0);

  const refresh = useCallback(async () => {
    try {
      setParts(await listParts(projectId));
    } catch (e) {
      setError(message(e));
    }
  }, [projectId]);

  useEffect(() => {
    setNotes(null);
    setDraft(null);
    void refresh();
  }, [refresh]);

  async function open(mpn: string) {
    const token = ++openToken.current;
    setError(null);
    setDraft(null);
    try {
      const n = await getNotes(projectId, mpn);
      if (token === openToken.current) setNotes(n);
    } catch (e) {
      if (token === openToken.current) setError(message(e));
    }
  }

  async function save() {
    if (!notes || draft === null) return;
    setBusy(true);
    setError(null);
    try {
      await saveNotes(projectId, notes.mpn, draft);
      await Promise.all([open(notes.mpn), refresh()]);
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }

  const current = parts?.find((p) => p.mpn === notes?.mpn);

  return (
    <div className="flex flex-col gap-3 p-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-muted">Parts researched for this project.</p>
        <button type="button" className={BUTTON} onClick={() => void refresh()}>
          Refresh
        </button>
      </div>
      {error && (
        <p role="alert" className="rounded border border-danger bg-danger/10 px-2 py-1 text-sm text-danger">
          {error}
        </p>
      )}
      {parts?.length === 0 && (
        <p className="text-sm text-muted">No researched parts yet. Ask the agent about a part number.</p>
      )}
      <ul aria-label="Parts" className="flex flex-col gap-1">
        {parts?.map((p) => (
          <li key={p.mpn}>
            <button
              type="button"
              aria-current={notes?.mpn === p.mpn ? 'true' : undefined}
              onClick={() => void open(p.mpn)}
              className={`flex w-full flex-wrap items-center gap-1.5 rounded px-2 py-1.5 text-left hover:bg-surface ${notes?.mpn === p.mpn ? 'bg-surface' : ''}`}
            >
              <span className="font-mono font-medium">{p.mpn}</span>
              {p.userEdited && <span className="rounded bg-surface-2 px-1.5 text-xs text-muted">edited by you</span>}
              {!p.hasNotes && <span className="text-xs text-muted">no notes</span>}
            </button>
          </li>
        ))}
      </ul>
      {notes && (
        <section aria-label={`Part ${notes.mpn}`} className="flex flex-col gap-2 border-t border-border pt-3">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="mr-auto font-mono font-semibold">{notes.mpn}</h3>
            {current?.hasDatasheet && (
              <a
                href={datasheetUrl(projectId, notes.mpn)}
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs text-accent underline"
              >
                Open datasheet
              </a>
            )}
            {draft === null ? (
              <button type="button" className={BUTTON} onClick={() => setDraft(notes.content)}>
                Edit
              </button>
            ) : (
              <>
                <button type="button" className={PRIMARY} disabled={busy} onClick={() => void save()}>
                  Save
                </button>
                <button type="button" className={BUTTON} disabled={busy} onClick={() => setDraft(null)}>
                  Cancel
                </button>
              </>
            )}
          </div>
          {draft === null ? (
            <article className="text-sm">
              <Markdown text={stripFrontMatter(notes.content)} />
            </article>
          ) : (
            <>
              <textarea
                aria-label="Notes"
                className="min-h-80 w-full rounded border border-border bg-surface p-2 font-mono text-xs"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
              />
              <p className="text-xs text-muted">
                Saved notes are marked as edited by you; the part researcher will not overwrite them.
              </p>
            </>
          )}
        </section>
      )}
    </div>
  );
}
