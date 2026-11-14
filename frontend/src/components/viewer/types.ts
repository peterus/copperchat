import type { DocKind } from '@/lib/viewUrls';
import { useComposer } from '@/stores/composer';
import type { ResolvedTheme } from '@/stores/ui';
import type { FindRequest, HighlightRequest } from '@/stores/view';

export type { FindRequest, HighlightRequest };

/** Outcome of applying a highlight on the page the pane opened for it. */
export interface HighlightResult {
  found: string[];
  missing: string[];
  /** The requested sheet instance does not exist in the loaded schematic. */
  sheetMissing?: boolean;
}

export interface KicadPaneProps {
  projectId: string;
  doc: DocKind;
  /**
   * Snapshot file names, root first (X3): the open response's `viewFiles[doc]`, replaced by the
   * `viewFiles` of later `view-updated` frames when sheets were added or removed.
   */
  files: readonly string[];
  /** Reload counter from the view store (`kicadRevs[doc]`); a change reloads the pane. */
  rev: number | null;
  /** Reload counter of the agent-view fallback (`revs[doc]`); default `rev`. */
  agentRev?: number | null;
  theme: ResolvedTheme;
  onPick: (ref: string) => void;
  find?: FindRequest | null;
  /**
   * Whether the pane is on screen (default true). A hidden pane defers reloads until it is shown
   * again, so only one KiCanvas instance loads at a time and load errors can be attributed.
   */
  visible?: boolean;
  /** `reason` explains a miss that is not "not in the document" (e.g. interactive view not loaded). */
  onFindResult?: (ref: string, found: boolean, reason?: string) => void;
  /** Schematic only: items to outline (E-C13); null clears. */
  highlight?: HighlightRequest | null;
  /** Once per highlight request, after its page is shown; `reason` when it cannot be shown at all. */
  onHighlightResult?: (result: HighlightResult, reason?: string) => void;
  /** Schematic only: the filename (`KcPage.filename`) of the page shown, on every page change. */
  onActiveSheet?: (file: string | null) => void;
  /** Schematic only: view files changed since this pane loaded them; marked in the sheet tree. */
  staleSheets?: readonly string[];
  /** Schematic only: the view store's `staleSeq` (bumped whenever a sheet is marked stale). */
  staleSeq?: number;
  /**
   * Schematic only: a full load (every file fetched fresh) is ready; `staleSeq` = the `staleSeq`
   * when that load started (sheets marked stale since are newer than what it fetched).
   */
  onLoaded?: (staleSeq: number) => void;
}

/** Tap on a part → `@REF ` in the composer. */
export function insertMention(ref: string): void {
  useComposer.getState().insertText(`@${ref}`);
}
