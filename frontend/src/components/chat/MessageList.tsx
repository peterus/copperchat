import { type ReactNode, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { MessageDTO } from '@/protocol/frames';

/** Within this distance of the bottom the list follows new content. */
export const STICK_THRESHOLD_PX = 80;

export interface MessageListProps {
  messages: MessageDTO[];
  hasMoreOlder: boolean;
  loadingOlder: boolean;
  onLoadOlder: () => void;
  renderItem: (m: MessageDTO) => ReactNode;
}

export function MessageList({ messages, hasMoreOlder, loadingOlder, onLoadOlder, renderItem }: MessageListProps) {
  const scrollerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const topSentinelRef = useRef<HTMLDivElement>(null);
  const anchorRef = useRef<{ id: number; offset: number } | null>(null);
  const prevFirstIdRef = useRef<number | null>(null);
  const [stuck, setStuck] = useState(true);
  const stuckRef = useRef(stuck);
  stuckRef.current = stuck;
  const firstId = messages[0]?.id ?? null;

  // Anchor = topmost (partially) visible message and its offset from the scroller top. Runs on
  // every scroll event (a prepend may commit before the next frame, so it cannot be deferred):
  // rows are stacked in id order, so a binary search needs only O(log n) layout reads.
  const captureAnchor = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const top = el.getBoundingClientRect().top;
    const nodes = el.querySelectorAll<HTMLElement>('[data-msg-id]');
    let lo = 0;
    let hi = nodes.length;
    let hit: { node: HTMLElement; top: number } | null = null;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      const node = nodes[mid]!;
      const r = node.getBoundingClientRect();
      if (r.bottom > top) {
        hit = { node, top: r.top };
        hi = mid;
      } else {
        lo = mid + 1;
      }
    }
    anchorRef.current = hit ? { id: Number(hit.node.dataset.msgId), offset: hit.top - top } : null;
  }, []);

  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const onScroll = () => {
      setStuck(el.scrollHeight - el.scrollTop - el.clientHeight < STICK_THRESHOLD_PX);
      captureAnchor();
    };
    el.addEventListener('scroll', onScroll, { passive: true });
    captureAnchor();
    return () => el.removeEventListener('scroll', onScroll);
  }, [captureAnchor]);

  // Lazy older load. Gated on "not stuck to bottom": on first mount the sentinel is briefly
  // visible before the auto-scroll lands; loading then would pin the user to old history.
  // Re-created on stuck/firstId change so a still-visible sentinel fires again.
  useEffect(() => {
    const el = topSentinelRef.current;
    if (!el || stuck) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) onLoadOlder();
      },
      { root: scrollerRef.current, rootMargin: '120px 0px 0px 0px' },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [onLoadOlder, stuck, hasMoreOlder, firstId]);

  // History shorter than the viewport cannot be scrolled, so "not stuck" never happens:
  // keep loading until it overflows. clientHeight 0 = not laid out yet (skip).
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el || !hasMoreOlder || loadingOlder || messages.length === 0) return;
    if (el.clientHeight > 0 && el.scrollHeight <= el.clientHeight) onLoadOlder();
  }, [messages, hasMoreOlder, loadingOlder, onLoadOlder]);

  // Element-based anchoring after a prepend (not height deltas: those drift by spinner height).
  useLayoutEffect(() => {
    const prev = prevFirstIdRef.current;
    prevFirstIdRef.current = firstId;
    if (prev === null || firstId === null || prev <= 0 || firstId <= 0 || firstId >= prev) return;
    const el = scrollerRef.current;
    const anchor = anchorRef.current;
    if (!el || !anchor) return;
    const node = el.querySelector<HTMLElement>(`[data-msg-id="${anchor.id}"]`);
    if (!node) return;
    const delta = node.getBoundingClientRect().top - el.getBoundingClientRect().top - anchor.offset;
    if (delta !== 0) el.scrollTop += delta;
    captureAnchor();
  }, [firstId, captureAnchor]);

  useEffect(() => {
    if (!stuck) return;
    const id = requestAnimationFrame(() => bottomRef.current?.scrollIntoView({ block: 'end' }));
    return () => cancelAnimationFrame(id);
  }, [messages, stuck]);

  // A hidden list (phone tab, collapsed sidebar: display none) cannot scroll; when it is shown
  // again (or resized) while stuck, re-stick so rows that arrived meanwhile are in view.
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => {
      if (stuckRef.current && el.clientHeight > 0) bottomRef.current?.scrollIntoView({ block: 'end' });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return (
    <div className="relative min-h-0 flex-1">
      {/* overflow-anchor: none — the anchoring above is the only mechanism (Safari has none,
          Chrome's native one would otherwise compete with it). */}
      <div
        ref={scrollerRef}
        data-testid="chat-scroller"
        role="log"
        aria-live="polite"
        className="absolute inset-0 overflow-y-auto overscroll-contain px-3 py-3 [overflow-anchor:none]"
      >
        {hasMoreOlder ? (
          <div ref={topSentinelRef} aria-hidden className="h-px" />
        ) : (
          messages.length > 0 && <p className="py-2 text-center text-xs text-muted">Start of conversation</p>
        )}
        {loadingOlder && <p className="py-2 text-center text-xs text-muted">Loading older messages…</p>}
        <ul className="space-y-3">
          {messages.map((m) => (
            <li key={m.id} data-msg-id={m.id}>
              {renderItem(m)}
            </li>
          ))}
        </ul>
        <div ref={bottomRef} aria-hidden />
      </div>
      {!stuck && (
        <button
          type="button"
          onClick={() => {
            setStuck(true);
            bottomRef.current?.scrollIntoView({ block: 'end' });
          }}
          className="absolute bottom-3 right-3 rounded-full bg-fg/85 px-3 py-1.5 text-xs text-bg shadow-lg"
        >
          ↓ newest
        </button>
      )}
    </div>
  );
}
