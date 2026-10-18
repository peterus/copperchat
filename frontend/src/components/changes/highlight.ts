import type { ChangeEntry, ChangesDiff } from '@/api/changes';
import type { HighlightKind } from '@/lib/kicanvas';
import type { HighlightBox, HighlightRef, HighlightRequest } from '@/stores/view';

export type HighlightInput = Omit<HighlightRequest, 'seq'>;

/** Change-log op → highlight kind (and the diff's own kinds). */
export const OP_KIND: Record<string, HighlightKind> = { add: 'added', modify: 'modified', delete: 'removed' };
export const kindOf = (op: string): HighlightKind => OP_KIND[op] ?? 'modified';

/** The diff box of an item on a sheet instance, if the diff has one. */
export function diffBox(diff: ChangesDiff | null | undefined, sheetPath: string, uuid: string): HighlightBox | null {
  const hit = diff?.sheets[sheetPath]?.find((d) => d.uuid === uuid && d.boxMm);
  return hit?.boxMm ? { ...hit.boxMm, kind: hit.kind, sheetPath } : null;
}

/**
 * Highlight items of change entries: live items by reference (or UUID: symbols/sheets), deleted
 * items by their diff box when the diff is loaded (they are gone from the document).
 */
function entryItems(e: ChangeEntry, diff: ChangesDiff | null | undefined): { ref?: HighlightRef; box?: HighlightBox } {
  const kind = kindOf(e.op);
  if (e.ref && e.op !== 'delete') return { ref: { ref: e.ref, kind, sheetPath: e.sheetPath } };
  const box = diffBox(diff, e.sheetPath, e.uuid);
  if (box) return { box: { ...box, kind } };
  return { ref: { ref: e.ref ?? e.uuid, kind, sheetPath: e.sheetPath } };
}

export function entriesHighlight(entries: ChangeEntry[], diff?: ChangesDiff | null): HighlightInput {
  // One item per sheet instance + UUID: the first op names it, a later delete wins; an item
  // placed and deleted again in the run is not shown.
  const byItem = new Map<string, ChangeEntry>();
  for (const e of entries) {
    const key = `${e.sheetPath}|${e.uuid}`;
    const prev = byItem.get(key);
    if (!prev) byItem.set(key, e);
    else if (e.op === 'delete') {
      if (prev.op === 'add') byItem.delete(key);
      else byItem.set(key, e);
    }
  }
  const refs: HighlightRef[] = [];
  const boxes: HighlightBox[] = [];
  for (const e of byItem.values()) {
    const { ref, box } = entryItems(e, diff);
    if (ref) refs.push(ref);
    if (box) boxes.push(box);
  }
  return { refs, boxes, sheetPath: entries[0]?.sheetPath ?? null };
}

/** The diff as a layer: every change as a box of its kind on its sheet instance. */
export function diffHighlight(diff: ChangesDiff): HighlightInput {
  const refs: HighlightRef[] = [];
  const boxes: HighlightBox[] = [];
  let first: string | null = null;
  for (const [sheetPath, list] of Object.entries(diff.sheets)) {
    if (list.length && first === null) first = sheetPath;
    for (const d of list) {
      if (d.boxMm) boxes.push({ ...d.boxMm, kind: d.kind, sheetPath });
      else if (d.ref) refs.push({ ref: d.ref, kind: d.kind, sheetPath });
    }
  }
  return { refs, boxes, sheetPath: first };
}
