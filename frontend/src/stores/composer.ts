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

/** A conversation's unsent message. */
export interface Draft {
  text: string;
  selection: Selection | null;
  attachments: AttachmentRef[];
}

/**
 * The composer edits the active conversation's draft (`text`, `selection`, `attachments`); the
 * drafts of the other conversations are parked in `drafts` (spec §12.1: keyed per conversation).
 */
export interface ComposerState {
  /** chatKey of the conversation the draft fields belong to, or null before one is shown. */
  activeKey: string | null;
  drafts: Readonly<Record<string, Draft>>;
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
  /** Parks the current draft and shows the draft of `key` (empty when it has none). */
  activate: (key: string) => void;
  /** Parks the current draft; no conversation is shown (what is typed now is dropped). */
  deactivate: () => void;
  /** Sets the attachments of draft `key`, active or parked (an upload that finishes after a switch). */
  setAttachmentsFor: (key: string, next: AttachmentRef[]) => void;
  /** Forgets every draft (logout). */
  reset: () => void;
}

const EMPTY_DRAFT: Draft = { text: '', selection: null, attachments: [] };

/** The drafts with the active one parked (an empty draft is dropped). */
function park(s: ComposerState): Record<string, Draft> {
  const parked = { ...s.drafts };
  if (s.activeKey !== null) {
    if (s.text === '' && s.attachments.length === 0) delete parked[s.activeKey];
    else parked[s.activeKey] = { text: s.text, selection: s.selection, attachments: s.attachments };
  }
  return parked;
}

export const useComposer = create<ComposerState>()((set, get) => ({
  activeKey: null,
  drafts: {},
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
  activate: (key) => {
    if (key === get().activeKey) return;
    const parked = park(get());
    const next = parked[key] ?? EMPTY_DRAFT;
    delete parked[key];
    set({ activeKey: key, drafts: parked, ...next, focusToken: 0 });
  },
  deactivate: () => {
    if (get().activeKey === null) return;
    set({ activeKey: null, drafts: park(get()), ...EMPTY_DRAFT, focusToken: 0 });
  },
  setAttachmentsFor: (key, next) => {
    if (key === get().activeKey) {
      set({ attachments: next });
      return;
    }
    const drafts = get().drafts;
    set({ drafts: { ...drafts, [key]: { ...(drafts[key] ?? EMPTY_DRAFT), attachments: next } } });
  },
  reset: () => set({ activeKey: null, drafts: {}, text: '', selection: null, attachments: [], focusToken: 0 }),
}));
