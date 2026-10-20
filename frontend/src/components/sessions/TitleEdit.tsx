import { useState } from 'react';

export const TITLE_MAX = 120;

/** A session title as the server takes it (spec §6.1: 1–120 chars, one line), or null. */
export function normalizeTitle(raw: string): string | null {
  const title = raw.replace(/\s+/g, ' ').trim();
  return title.length >= 1 && title.length <= TITLE_MAX ? title : null;
}

/** Inline title editor: Enter saves, Escape cancels; an invalid title is not sent. */
export function TitleEdit({
  initial,
  onSave,
  onCancel,
}: {
  initial: string;
  /** Rejecting shows the error and keeps the editor open. */
  onSave: (title: string) => Promise<void>;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function save() {
    const title = normalizeTitle(value);
    if (title === null) {
      setError(`A title has 1–${TITLE_MAX} characters.`);
      return;
    }
    if (title === initial) {
      onCancel();
      return;
    }
    setBusy(true);
    try {
      await onSave(title);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <span className="flex min-w-0 flex-1 flex-col">
      <input
        aria-label="Session title"
        autoFocus
        value={value}
        disabled={busy}
        maxLength={TITLE_MAX * 2}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            void save();
          } else if (e.key === 'Escape') {
            e.preventDefault();
            e.stopPropagation();
            onCancel();
          }
        }}
        className="min-w-0 rounded border border-border bg-surface px-2 py-0.5 text-sm"
      />
      {error && (
        <span role="alert" className="text-xs text-danger">
          {error}
        </span>
      )}
    </span>
  );
}
