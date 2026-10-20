import type { DocIndexDTO } from '@/api/docs';
import { BADGE_CLASS, changedBy, docBadge, docName } from './format';

/** The five fixed docs, then extra docs, then files that are not read (spec §12.2). */
export function DocIndex({
  index,
  onOpen,
  drafts,
}: {
  index: DocIndexDTO;
  onOpen: (path: string) => void;
  /** Paths with an unsaved editor draft. */
  drafts?: ReadonlySet<string>;
}) {
  return (
    <div className="flex flex-col gap-3 p-3">
      <ul aria-label="Design docs" className="flex flex-col gap-1">
        {index.docs.map((d) => {
          const badge = docBadge(d);
          const by = changedBy(d.lastAuthor, d.lastConversationTitle);
          return (
            <li key={d.path}>
              <button
                type="button"
                onClick={() => onOpen(d.path)}
                className="flex w-full flex-col gap-0.5 rounded px-2 py-1.5 text-left hover:bg-surface"
              >
                <span className="flex flex-wrap items-center gap-1.5">
                  <span className="font-mono font-medium">{docName(d.path)}</span>
                  <span className={`rounded border px-1.5 text-xs ${BADGE_CLASS[badge]}`}>{badge}</span>
                  {drafts?.has(d.path) && (
                    <span className="rounded border border-warning px-1.5 text-xs text-warning">unsaved draft</span>
                  )}
                  {d.userEdited && <span className="rounded bg-surface-2 px-1.5 text-xs text-muted">edited by you</span>}
                  {d.openPoints > 0 && (
                    <span className="text-xs text-muted">
                      {d.openPoints} open {d.openPoints === 1 ? 'point' : 'points'}
                    </span>
                  )}
                </span>
                {by && <span className="text-xs text-muted">Last change: {by}</span>}
              </button>
            </li>
          );
        })}
      </ul>
      {index.ignored.length > 0 && (
        <section aria-label="Ignored files" className="border-t border-border pt-2 text-xs text-muted">
          <p>Not read:</p>
          <ul>
            {index.ignored.map((i) => (
              <li key={i.path}>
                <span className="font-mono">{docName(i.path)}</span> — {i.reason}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
