import { create } from 'zustand';
import type { AttachmentRef } from '@/protocol/frames';

export type Selection = { start: number; end: number };

/**
 * Inserts `insert` at the selection (or appends), adding a separating space before it when needed
 * and one after it unless the following text already starts with whitespace.
 */
export function insertAt(text: string, insert: string, selection: Selection | null): { text: string; caret: number } {
  const start = Math.min(selection?.start ?? text.length, text.length);
  const end = Math.min(Math.max(selection?.end ?? start, start), text.length);
  const before = text.slice(0, start);
  const after = text.slice(end);
  const lead = before.length > 0 && !/\s$/.test(before) ? ' ' : '';
  const trail = after.length > 0 && /^\s/.test(after) ? '' : ' ';
  const head = before + lead + insert + trail;
  // When the following text already starts with a space, the caret lands after that space.
  const caret = head.length + (trail === '' && after.startsWith(' ') ? 1 : 0);
  return { text: head + after, caret };
}

export interface ComposerState {
  text: string;
  selection: Selection | null;
  attachments: AttachmentRef[];
  /** Bumped by insertText(); the Composer focuses the textarea and restores the caret on change. */
  focusToken: number;
  setText: (text: string) => void;
  setSelection: (start: number, end: number) => void;
  /** Inserts at the caret with surrounding spaces and focuses the textarea (viewer @REF mentions). */
  insertText: (text: string) => void;
  addAttachment: (a: AttachmentRef) => void;
  removeAttachment: (id: string) => void;
  setAttachments: (next: AttachmentRef[]) => void;
  clear: () => void;
}

export const useComposer = create<ComposerState>()((set, get) => ({
  text: '',
  selection: null,
  attachments: [],
  focusToken: 0,
  setText: (text) => set({ text }),
  setSelection: (start, end) => set({ selection: { start, end } }),
  insertText: (insert) => {
    const { text, caret } = insertAt(get().text, insert, get().selection);
    set({ text, selection: { start: caret, end: caret }, focusToken: get().focusToken + 1 });
  },
  addAttachment: (a) => {
    if (get().attachments.some((x) => x.id === a.id)) return;
    set({ attachments: [...get().attachments, a] });
  },
  removeAttachment: (id) => set({ attachments: get().attachments.filter((a) => a.id !== id) }),
  setAttachments: (next) => set({ attachments: next }),
  clear: () => set({ text: '', selection: null, attachments: [], focusToken: 0 }),
}));
