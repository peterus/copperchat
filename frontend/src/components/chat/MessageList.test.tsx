import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { MessageDTO } from '@/protocol/frames';
import { row } from '@/test/fakeMessageServer';
import { MessageList } from './MessageList';

const ROW = 100;
const VIEWPORT = 500;
let scrollTop = 0;

class FakeIO {
  static instances: FakeIO[] = [];
  constructor(public cb: IntersectionObserverCallback) {
    FakeIO.instances.push(this);
  }
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
  fire(isIntersecting: boolean) {
    this.cb([{ isIntersecting } as IntersectionObserverEntry], this as unknown as IntersectionObserver);
  }
}

function rect(top: number, height: number): DOMRect {
  return { top, bottom: top + height, left: 0, right: 300, width: 300, height, x: 0, y: top, toJSON: () => ({}) } as DOMRect;
}

function rows(from: number, to: number): MessageDTO[] {
  return Array.from({ length: to - from + 1 }, (_, i) => row(from + i));
}

const renderItem = (m: MessageDTO) => <span>{m.content}</span>;

function renderList(messages: MessageDTO[], onLoadOlder = vi.fn()) {
  const view = render(
    <MessageList messages={messages} hasMoreOlder loadingOlder={false} onLoadOlder={onLoadOlder} renderItem={renderItem} />,
  );
  const scroller = screen.getByTestId('chat-scroller');
  Object.defineProperty(scroller, 'scrollTop', { configurable: true, get: () => scrollTop, set: (v: number) => { scrollTop = v; } });
  Object.defineProperty(scroller, 'clientHeight', { configurable: true, get: () => VIEWPORT });
  Object.defineProperty(scroller, 'scrollHeight', {
    configurable: true,
    get: () => scroller.querySelectorAll('[data-msg-id]').length * ROW,
  });
  return { ...view, scroller, onLoadOlder };
}

function scrollTo(scroller: HTMLElement, top: number) {
  scrollTop = top;
  fireEvent.scroll(scroller);
}

beforeEach(() => {
  scrollTop = 0;
  FakeIO.instances = [];
  vi.stubGlobal('IntersectionObserver', FakeIO);
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    if (this.dataset.testid === 'chat-scroller') return rect(0, VIEWPORT);
    if (this.dataset.msgId !== undefined) {
      const all = Array.from(document.querySelectorAll('[data-msg-id]'));
      return rect(all.indexOf(this) * ROW - scrollTop, ROW);
    }
    return rect(0, 0);
  });
});
afterEach(() => vi.useRealTimers());

describe('MessageList', () => {
  it('keeps the topmost visible message in place when older messages are prepended', () => {
    const { scroller, rerender } = renderList(rows(51, 60));
    scrollTo(scroller, 250); // row index 2 (id 53) is topmost visible, offset -50
    rerender(
      <MessageList messages={rows(1, 60)} hasMoreOlder loadingOlder={false} onLoadOlder={vi.fn()} renderItem={renderItem} />,
    );
    // id 53 moved from index 2 to index 52: +5000 px
    expect(scrollTop).toBe(5250);
  });

  it('anchors to where the user scrolled during the fetch, not where the fetch started', () => {
    const { scroller, rerender } = renderList(rows(51, 60));
    scrollTo(scroller, 250);
    rerender(<MessageList messages={rows(51, 60)} hasMoreOlder loadingOlder onLoadOlder={vi.fn()} renderItem={renderItem} />);
    scrollTo(scroller, 120); // id 52 topmost, offset -20
    rerender(
      <MessageList messages={rows(41, 60)} hasMoreOlder loadingOlder={false} onLoadOlder={vi.fn()} renderItem={renderItem} />,
    );
    // id 52 moved from index 1 to index 11: +1000 px
    expect(scrollTop).toBe(1120);
  });

  it('does not move the scroll position when rows are appended at the bottom', () => {
    const { scroller, rerender } = renderList(rows(51, 60));
    scrollTo(scroller, 250);
    rerender(
      <MessageList messages={rows(51, 62)} hasMoreOlder loadingOlder={false} onLoadOlder={vi.fn()} renderItem={renderItem} />,
    );
    expect(scrollTop).toBe(250);
  });

  it('sticks to the bottom within 80 px and shows the newest pill otherwise', async () => {
    const spy = vi.spyOn(Element.prototype, 'scrollIntoView');
    const { scroller } = renderList(rows(1, 10));
    await act(async () => new Promise((r) => requestAnimationFrame(() => r(undefined))));
    expect(spy).toHaveBeenCalled();
    scrollTo(scroller, 1000 - VIEWPORT - 79);
    expect(screen.queryByRole('button', { name: '↓ newest' })).toBeNull();
    scrollTo(scroller, 100);
    const pill = screen.getByRole('button', { name: '↓ newest' });
    spy.mockClear();
    fireEvent.click(pill);
    expect(spy).toHaveBeenCalledWith({ block: 'end' });
    expect(screen.queryByRole('button', { name: '↓ newest' })).toBeNull();
  });

  it('does not auto-scroll on new messages while the user reads older history', async () => {
    const spy = vi.spyOn(Element.prototype, 'scrollIntoView');
    const { scroller, rerender } = renderList(rows(1, 10));
    await act(async () => new Promise((r) => requestAnimationFrame(() => r(undefined))));
    scrollTo(scroller, 100);
    spy.mockClear();
    rerender(<MessageList messages={rows(1, 11)} hasMoreOlder loadingOlder={false} onLoadOlder={vi.fn()} renderItem={renderItem} />);
    await act(async () => new Promise((r) => requestAnimationFrame(() => r(undefined))));
    expect(spy).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '↓ newest' })).toBeInTheDocument();
  });

  it('loads older pages from the top sentinel only when not stuck to the bottom', () => {
    const { scroller, onLoadOlder } = renderList(rows(1, 10));
    expect(FakeIO.instances).toHaveLength(0);
    scrollTo(scroller, 0);
    FakeIO.instances.at(-1)!.fire(true);
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    FakeIO.instances.at(-1)!.fire(false);
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
  });

  it('loads older pages when the history does not fill the viewport (no scroll possible)', () => {
    const onLoadOlder = vi.fn();
    const { rerender } = renderList(rows(1, 3), onLoadOlder);
    expect(onLoadOlder).not.toHaveBeenCalled(); // no layout yet (clientHeight 0 on first render)
    rerender(<MessageList messages={rows(1, 4)} hasMoreOlder loadingOlder={false} onLoadOlder={onLoadOlder} renderItem={renderItem} />);
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    rerender(<MessageList messages={rows(1, 4)} hasMoreOlder loadingOlder onLoadOlder={onLoadOlder} renderItem={renderItem} />);
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
  });

  it('shows the start marker when there is nothing older', () => {
    render(<MessageList messages={rows(1, 2)} hasMoreOlder={false} loadingOlder={false} onLoadOlder={vi.fn()} renderItem={renderItem} />);
    expect(screen.getByText('Start of conversation')).toBeInTheDocument();
  });

  it('shows a loading hint while older messages are fetched', () => {
    render(<MessageList messages={rows(1, 2)} hasMoreOlder loadingOlder onLoadOlder={vi.fn()} renderItem={renderItem} />);
    expect(screen.getByText('Loading older messages…')).toBeInTheDocument();
  });
});
