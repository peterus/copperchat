import { useEffect, useId, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { getDocIndex, type DocIndexEntry } from '@/api/docs';
import { usePanels } from '@/layouts/panels';
import type { ConversationType } from '@/protocol/frames';
import { useConversationsApi } from '@/stores/conversations';
import { useUi } from '@/stores/ui';
import { sessionPath } from './SessionList';
import { normalizeTitle, TITLE_MAX } from './TitleEdit';
import { TYPE_LABEL, TypeIcon } from './TypeIcon';

export const REQUIREMENTS_PATH = 'docs/design/requirements.md';

const CARDS: ReadonlyArray<{ type: ConversationType; text: string; disabled?: boolean }> = [
  { type: 'requirements', text: 'Works out the requirements with you and writes the design docs; reads the design, never edits it.' },
  { type: 'schematic', text: 'Edits the schematic from the requirements; records decisions and open points.' },
  { type: 'free', text: 'Every tool: questions, research and edits without a fixed phase.' },
  { type: 'board', text: 'Coming with board editing.', disabled: true },
];

type Requirements = { state: 'loading' } | { state: 'failed' } | { state: 'known'; entry: DocIndexEntry | null };

const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? '' : 's'}`;

/** The gate warning for a Schematic session (spec §11), or null when requirements are approved. */
function gateWarning(req: Requirements): string | null {
  if (req.state === 'loading') return null;
  if (req.state === 'failed') return 'Could not check the requirements. The agent may work from unapproved requirements.';
  const e = req.entry;
  if (!e?.exists) return 'Requirements are missing (no requirements.md). The agent will work without written requirements.';
  if (e.approved && !e.changedSinceApproval) return null;
  const why = e.approved ? 'changed since approval' : 'draft';
  return `Requirements are not approved (${why}, ${plural(e.openPoints, 'open point')}). The agent will work from unapproved requirements.`;
}

/** Type cards, an optional title and the soft phase gate (spec §11, §12.1). */
export function NewSessionDialog({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const titleId = useId();
  const store = useConversationsApi();
  const navigate = useNavigate();
  const openPanel = useUi((s) => s.openPanel);
  const docsPanel = usePanels().some((p) => p.id === 'docs');
  const [req, setReq] = useState<Requirements>({ state: 'loading' });
  const [type, setType] = useState<ConversationType | null>(null);
  const [title, setTitle] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    getDocIndex(projectId).then(
      (index) => {
        if (!live) return;
        const entry = index.docs.find((d) => d.path === REQUIREMENTS_PATH) ?? null;
        setReq({ state: 'known', entry });
        setType((t) => t ?? (entry?.exists ? 'schematic' : 'requirements'));
      },
      () => {
        if (!live) return;
        setReq({ state: 'failed' });
        setType((t) => t ?? 'requirements');
      },
    );
    return () => {
      live = false;
    };
  }, [projectId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [busy, onClose]);

  const warning = type === 'schematic' ? gateWarning(req) : null;
  const trimmed = title.trim() === '' ? undefined : normalizeTitle(title);

  async function start() {
    if (type === null || trimmed === null) return;
    setBusy(true);
    setError(null);
    try {
      const c = await store.getState().create(projectId, trimmed === undefined ? { type } : { type, title: trimmed });
      onClose();
      navigate(sessionPath(projectId, c.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <div role="dialog" aria-modal="true" aria-labelledby={titleId} className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="flex max-h-full w-full max-w-lg flex-col gap-3 overflow-y-auto rounded border border-border bg-surface p-4 text-fg shadow-lg">
        <h2 id={titleId} className="font-semibold">
          New session
        </h2>
        <div role="radiogroup" aria-label="Session type" className="grid gap-2 sm:grid-cols-2">
          {CARDS.map((c) => (
            <label
              key={c.type}
              className={`flex cursor-pointer gap-2 rounded border p-2 text-sm ${type === c.type ? 'border-accent bg-accent/10' : 'border-border'} ${c.disabled ? 'cursor-not-allowed opacity-50' : ''}`}
            >
              <input
                type="radio"
                name={`${titleId}-type`}
                className="sr-only"
                checked={type === c.type}
                disabled={c.disabled || busy}
                onChange={() => setType(c.type)}
                aria-label={`${TYPE_LABEL[c.type]}: ${c.text}`}
              />
              <TypeIcon type={c.type} className="mt-0.5 h-4 w-4" />
              <span>
                <span className="block font-medium">{TYPE_LABEL[c.type]}</span>
                <span className="block text-xs text-muted">{c.text}</span>
              </span>
            </label>
          ))}
        </div>
        <label className="flex flex-col gap-1 text-sm">
          Title (optional)
          <input
            value={title}
            disabled={busy}
            maxLength={TITLE_MAX * 2}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Session N"
            className="rounded border border-border bg-bg px-2 py-1"
          />
          {trimmed === null && <span className="text-xs text-danger">A title has 1–{TITLE_MAX} characters.</span>}
        </label>
        {warning && (
          <p role="alert" className="rounded bg-warning/10 p-2 text-sm">
            {warning}
          </p>
        )}
        {error && (
          <p role="alert" className="rounded bg-danger/10 p-2 text-sm text-danger">
            {error}
          </p>
        )}
        <div className="flex flex-wrap justify-end gap-2">
          <button type="button" onClick={onClose} disabled={busy} className="rounded border border-border px-3 py-1">
            Cancel
          </button>
          {warning && docsPanel && (
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                openPanel('docs', { path: REQUIREMENTS_PATH });
                onClose();
              }}
              className="rounded border border-border px-3 py-1"
            >
              Open requirements
            </button>
          )}
          <button
            type="button"
            disabled={busy || type === null || trimmed === null}
            onClick={() => void start()}
            className="rounded bg-accent px-3 py-1 text-accent-fg disabled:opacity-50"
          >
            {busy ? 'Starting…' : warning ? 'Start anyway' : 'Start session'}
          </button>
        </div>
      </div>
    </div>
  );
}
