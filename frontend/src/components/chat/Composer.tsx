import { type FormEvent, type KeyboardEvent, useEffect, useRef } from 'react';
import { useKeyboardInset } from '@/hooks/useKeyboardInset';
import { useMediaQuery } from '@/hooks/useMediaQuery';
import type { AttachmentRef, MessageDTO } from '@/protocol/frames';
import { useComposer } from '@/stores/composer';
import { useComposerAttachmentSlot } from './composerSlots';
import { RunControls } from './RunControls';

export interface ComposerProps {
  projectId?: string;
  runActive: boolean;
  /** Server-acknowledged user rows waiting for the agent (cancellable). */
  queued: MessageDTO[];
  disabled?: boolean;
  /** Returning false keeps the draft (the message was refused locally). */
  onSend: (text: string, attachments: AttachmentRef[]) => boolean | void;
  onStop: () => void;
  onCancelQueued: (id: number) => void;
}

export function Composer({ projectId = '', runActive, queued, disabled = false, onSend, onStop, onCancelQueued }: ComposerProps) {
  const text = useComposer((s) => s.text);
  const attachments = useComposer((s) => s.attachments);
  const focusToken = useComposer((s) => s.focusToken);
  const setText = useComposer((s) => s.setText);
  const setSelection = useComposer((s) => s.setSelection);
  const setAttachments = useComposer((s) => s.setAttachments);
  const removeAttachment = useComposer((s) => s.removeAttachment);
  const clear = useComposer((s) => s.clear);
  const ref = useRef<HTMLTextAreaElement>(null);
  const coarse = useMediaQuery('(pointer: coarse)');
  const keyboardInset = useKeyboardInset();
  const Slot = useComposerAttachmentSlot();

  useEffect(() => {
    const el = ref.current;
    if (!el || focusToken === 0) return;
    el.focus();
    const sel = useComposer.getState().selection;
    if (sel) el.setSelectionRange(sel.start, sel.end);
  }, [focusToken]);

  // The send frame requires non-empty text (backend SendFrame.text min_length=1), also with attachments.
  const canSend = !disabled && text.trim() !== '';

  function submit() {
    if (!canSend) return;
    if (onSend(text.trim(), attachments) === false) return;
    clear();
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // Touch keyboards: Enter is a newline, sending is via the button.
    if (e.key === 'Enter' && !e.shiftKey && !coarse && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  }

  return (
    <div
      data-testid="composer"
      className="border-t border-border bg-bg px-3 pt-2"
      style={{
        transform: keyboardInset ? `translateY(-${keyboardInset}px)` : undefined,
        paddingBottom: 'max(0.5rem, env(safe-area-inset-bottom))',
      }}
    >
      {queued.length > 0 && (
        <ul aria-label="Queued messages" className="mb-2 space-y-1">
          {queued.map((q) => (
            <li key={q.id} className="flex items-center gap-2 rounded bg-surface px-2 py-1 text-sm">
              <span className="text-xs text-muted">queued</span>
              <span className="min-w-0 flex-1 truncate">{q.content}</span>
              <button
                type="button"
                aria-label={`Cancel queued message: ${q.content}`}
                onClick={() => onCancelQueued(q.id)}
                className="text-xs text-danger"
              >
                Cancel
              </button>
            </li>
          ))}
        </ul>
      )}
      {attachments.length > 0 && (
        <ul aria-label="Attachments" className="mb-2 flex flex-wrap gap-1">
          {attachments.map((a) => (
            <li key={a.id} className="flex items-center gap-1 rounded bg-surface-2 px-2 py-0.5 text-xs">
              {a.name}
              <button type="button" aria-label={`Remove ${a.name}`} onClick={() => removeAttachment(a.id)}>
                ✕
              </button>
            </li>
          ))}
        </ul>
      )}
      <form
        className="flex items-end gap-2"
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          submit();
        }}
      >
        {Slot && (
          <Slot disabled={disabled} projectId={projectId} attachments={attachments} setAttachments={setAttachments} />
        )}
        <textarea
          ref={ref}
          aria-label="Message"
          rows={1}
          value={text}
          disabled={disabled}
          placeholder="Ask about this design…"
          onChange={(e) => setText(e.target.value)}
          onSelect={(e) => setSelection(e.currentTarget.selectionStart, e.currentTarget.selectionEnd)}
          onKeyDown={onKeyDown}
          className="max-h-40 min-h-10 flex-1 resize-none rounded-lg border border-border bg-surface px-3 py-2 field-sizing-content"
        />
        <RunControls runActive={runActive} onStop={onStop} />
        <button
          type="submit"
          disabled={!canSend}
          className="shrink-0 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-accent-fg disabled:opacity-50"
        >
          Send
        </button>
      </form>
    </div>
  );
}
