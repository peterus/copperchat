/** The session an entry came from (History, Changes; spec §12.4); nothing for pre-session entries. */
export function SessionChip({ title }: { title: string | null | undefined }) {
  if (!title) return null;
  return (
    <span aria-label="Session" title={title} className="inline-block max-w-40 truncate rounded bg-surface-2 px-1.5 align-middle text-xs text-muted">
      {title}
    </span>
  );
}
