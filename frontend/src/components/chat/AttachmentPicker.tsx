import { Camera, LoaderCircle, Paperclip } from 'lucide-react';
import { type ChangeEvent, useEffect, useRef, useState } from 'react';
import { uploadAttachment } from '@/api/uploads';
import type { ComposerAttachmentSlotProps } from './composerSlots';

const ICON_BUTTON =
  'flex h-10 w-10 shrink-0 cursor-pointer items-center justify-center rounded-lg border border-border bg-surface text-muted hover:text-fg focus-within:ring-2 focus-within:ring-accent has-[:disabled]:cursor-not-allowed has-[:disabled]:opacity-50';

/**
 * Composer slot (Phase 8): uploads picked files / camera photos and appends their AttachmentRefs.
 * The chips (with remove buttons) are rendered by the Composer itself.
 */
export function AttachmentPicker({ disabled, projectId, attachments, setAttachments }: ComposerAttachmentSlotProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Several files upload one after another; each appends to the latest list, not a stale prop.
  const latest = useRef(attachments);
  useEffect(() => {
    latest.current = attachments;
  }, [attachments]);

  async function onFiles(e: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files ?? []);
    e.target.value = '';
    if (files.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      for (const file of files) {
        const ref = await uploadAttachment(projectId, file);
        latest.current = [...latest.current, ref];
        setAttachments(latest.current);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const off = disabled || busy;
  return (
    <div className="flex shrink-0 items-end gap-1">
      <label className={ICON_BUTTON} title="Attach file">
        <Paperclip className="h-4 w-4" aria-hidden />
        <input
          aria-label="Attach file"
          type="file"
          multiple
          accept="image/*,application/pdf"
          className="sr-only"
          disabled={off}
          onChange={(e) => void onFiles(e)}
        />
      </label>
      <label className={ICON_BUTTON} title="Take photo">
        <Camera className="h-4 w-4" aria-hidden />
        <input
          aria-label="Take photo"
          type="file"
          accept="image/*"
          capture="environment"
          className="sr-only"
          disabled={off}
          onChange={(e) => void onFiles(e)}
        />
      </label>
      {busy && (
        <span role="status" aria-label="Uploading" className="flex h-10 items-center text-muted">
          <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
        </span>
      )}
      {error && (
        <span role="alert" className="self-center max-w-40 text-xs text-danger">
          {error}
        </span>
      )}
    </div>
  );
}
