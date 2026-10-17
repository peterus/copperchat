import { memo, useCallback, useState } from 'react';
import { isUploadId, uploadUrl } from '@/api/uploads';
import type { MessageDTO } from '@/protocol/frames';
import { Lightbox } from './Lightbox';
import { Markdown } from './Markdown';

/**
 * Metadata image urls come from the backend (`/api/projects/{id}/images/{name}`, engine._store_image),
 * not from agent text: same-origin API paths or local blobs. Markdown images have a stricter rule.
 */
export const isSafeImageUrl = (u: string) => u.startsWith('/api/') || u.startsWith('blob:');

/** Image urls of a row: `metadata.url` plus `metadata.images` (strings or `{url}`), foreign origins removed. */
export function imageUrls(m: MessageDTO): string[] {
  const out: string[] = [];
  const md = m.metadata;
  if (typeof md.url === 'string') out.push(md.url);
  if (Array.isArray(md.images)) {
    for (const img of md.images as unknown[]) {
      if (typeof img === 'string') out.push(img);
      else if (img && typeof img === 'object' && typeof (img as { url?: unknown }).url === 'string') {
        out.push((img as { url: string }).url);
      }
    }
  }
  return out.filter(isSafeImageUrl);
}

/**
 * The backend persists a tool result's images twice: as `metadata.images` on the tool_result row
 * and as separate `image` rows carrying the same `toolUseId`. When that tool_result is loaded its
 * thumbnails already show them, so the duplicate image rows are dropped. Image rows whose result
 * is not loaded (older page) stay visible.
 */
export function withoutDuplicateToolImages(messages: MessageDTO[]): MessageDTO[] {
  const shown = new Map<string, Set<string>>();
  for (const m of messages) {
    const id = m.metadata.toolUseId;
    if (m.kind === 'tool_result' && typeof id === 'string') shown.set(id, new Set(imageUrls(m)));
  }
  if (shown.size === 0) return messages;
  const out = messages.filter((m) => {
    if (m.kind !== 'image' || typeof m.metadata.toolUseId !== 'string') return true;
    const urls = shown.get(m.metadata.toolUseId);
    const own = imageUrls(m);
    return !urls || own.length === 0 || own.some((u) => !urls.has(u));
  });
  return out.length === messages.length ? messages : out;
}

function Thumbnails({ urls }: { urls: string[] }) {
  const [open, setOpen] = useState<string | null>(null);
  const close = useCallback(() => setOpen(null), []);
  if (urls.length === 0) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {urls.map((u, i) => (
        <button
          key={u}
          type="button"
          aria-label={`Open image ${i + 1}`}
          onClick={() => setOpen(u)}
          className="overflow-hidden rounded border border-border"
        >
          <img src={u} alt="" loading="lazy" className="h-24 w-auto max-w-48 object-contain" />
        </button>
      ))}
      {open && <Lightbox src={open} alt="Agent view" onClose={close} />}
    </div>
  );
}

interface SentAttachment {
  id: string;
  kind: 'image' | 'pdf';
  name: string;
}

/** `metadata.attachments` of a user row; entries with an unexpected shape or id are dropped. */
export function sentAttachments(m: MessageDTO): SentAttachment[] {
  const raw = m.metadata.attachments;
  if (!Array.isArray(raw)) return [];
  return (raw as unknown[]).filter((a): a is SentAttachment => {
    if (!a || typeof a !== 'object') return false;
    const { id, kind, name } = a as Record<string, unknown>;
    return typeof id === 'string' && isUploadId(id) && (kind === 'image' || kind === 'pdf') && typeof name === 'string';
  });
}

function AttachmentChips({ projectId, items }: { projectId: string; items: SentAttachment[] }) {
  if (items.length === 0) return null;
  return (
    <ul aria-label="Sent attachments" className="mt-1 flex flex-wrap justify-end gap-1">
      {items.map((a) => (
        <li key={a.id} className="flex items-center gap-1 rounded bg-black/15 px-1.5 py-0.5 text-xs">
          {a.kind === 'image' ? (
            <img src={uploadUrl(projectId, a.id)} alt="" loading="lazy" className="h-8 w-8 rounded object-cover" />
          ) : (
            <span className="font-semibold">PDF</span>
          )}
          <span className="max-w-40 truncate">{a.name}</span>
        </li>
      ))}
    </ul>
  );
}

function toolName(m: MessageDTO): string {
  const n = m.metadata.name ?? m.metadata.toolName;
  return typeof n === 'string' ? n : m.content || 'tool';
}

function oneLine(input: unknown): string {
  const s = JSON.stringify(input ?? {});
  return s.length > 80 ? `${s.slice(0, 77)}…` : s;
}

/** Memoized: rows keep their identity in the store unless they change (see chat.ts patch()). */
export const MessageItem = memo(function MessageItem({ message: m }: { message: MessageDTO }) {
  const streaming = m.status === 'streaming';
  switch (m.kind) {
    case 'text':
      if (m.role === 'user') {
        return (
          <div className="flex justify-end" data-role="user">
            <div
              className={`max-w-[85%] rounded-2xl bg-accent px-3 py-2 text-accent-fg ${m.status === 'cancelled' ? 'opacity-50 line-through' : ''}`}
            >
              <p className="whitespace-pre-wrap break-words">{m.content}</p>
              <AttachmentChips projectId={m.projectId} items={sentAttachments(m)} />
              {m.metadata.pending === true && m.status !== 'error' && <p className="mt-1 text-xs opacity-75">sending…</p>}
              {m.status === 'error' && <p className="mt-1 text-xs">not sent: {String(m.metadata.error ?? 'error')}</p>}
            </div>
          </div>
        );
      }
      return (
        <div data-role={m.role} className="max-w-full">
          <Markdown text={m.content} />
          {streaming && (
            <span aria-label="streaming" className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-fg/60 align-middle" />
          )}
        </div>
      );
    case 'thinking':
      return (
        <details className="rounded border border-border bg-surface px-3 py-2 text-sm text-muted" data-role="assistant">
          <summary className="cursor-pointer select-none">Thinking</summary>
          <p className="mt-2 whitespace-pre-wrap">{m.content}</p>
        </details>
      );
    case 'tool_use':
      return (
        <details className="rounded border border-border bg-surface px-3 py-2 text-sm" data-role="assistant">
          <summary className="cursor-pointer select-none font-mono">
            {toolName(m)} <span className="text-muted">{oneLine(m.metadata.input)}</span>
          </summary>
          <pre className="mt-2 overflow-x-auto text-xs">{JSON.stringify(m.metadata.input ?? {}, null, 2)}</pre>
        </details>
      );
    case 'tool_result': {
      const isError = m.metadata.isError === true;
      return (
        <div data-role="assistant">
          <details className={`rounded border px-3 py-2 text-sm ${isError ? 'border-danger' : 'border-border'} bg-surface`}>
            <summary className={`cursor-pointer select-none ${isError ? 'text-danger' : ''}`}>
              {isError ? 'Tool error' : 'Tool result'}
            </summary>
            <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap text-xs">{m.content}</pre>
          </details>
          <Thumbnails urls={imageUrls(m)} />
        </div>
      );
    }
    case 'image':
      return (
        <div data-role={m.role} className={m.role === 'user' ? 'flex flex-col items-end' : undefined}>
          {m.content && <p className="text-sm text-muted">{m.content}</p>}
          <Thumbnails urls={imageUrls(m)} />
        </div>
      );
    case 'error':
      // Generic on purpose (R3): kicad_unavailable, agent_unavailable and future codes all show their text.
      return (
        <div
          role="alert"
          data-code={typeof m.metadata.code === 'string' ? m.metadata.code : undefined}
          className="rounded border border-danger bg-danger/10 px-3 py-2 text-sm text-danger"
        >
          {m.content}
        </div>
      );
    case 'notice':
      return <p className="text-center text-xs text-muted">{m.content}</p>;
  }
});
