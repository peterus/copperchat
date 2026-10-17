import type { DocKind } from '@/lib/viewUrls';
import { useComposer } from '@/stores/composer';
import type { ResolvedTheme } from '@/stores/ui';

/** "Find REF" request; `seq` makes repeated searches for the same ref distinct. */
export interface FindRequest {
  ref: string;
  seq: number;
}

export interface KicadPaneProps {
  projectId: string;
  doc: DocKind;
  /** Snapshot file names from the open response's `viewFiles[doc]`, root first (X3). */
  files: readonly string[];
  /** Reload counter from the view store (`revs[doc]`); a change reloads the pane. */
  rev: number | null;
  theme: ResolvedTheme;
  onPick: (ref: string) => void;
  find?: FindRequest | null;
  onFindResult?: (ref: string, found: boolean) => void;
}

/** Tap on a part → `@REF ` in the composer. */
export function insertMention(ref: string): void {
  useComposer.getState().insertText(`@${ref}`);
}
