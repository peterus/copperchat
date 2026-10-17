type Matcher = (query: string) => boolean;
type Listener = (event: MediaQueryListEvent) => void;

let matcher: Matcher = () => false;
const lists: Array<{ query: string; listeners: Set<Listener> }> = [];

/** Change which media queries match; notifies every live MediaQueryList listener. */
export function setMedia(next: Matcher): void {
  matcher = next;
  for (const entry of lists) {
    const event = { matches: matcher(entry.query), media: entry.query } as MediaQueryListEvent;
    for (const listener of [...entry.listeners]) listener(event);
  }
}

export function resetMedia(): void {
  matcher = () => false;
  lists.length = 0;
}

export function installMatchMedia(): void {
  window.matchMedia = (query: string): MediaQueryList => {
    const entry = { query, listeners: new Set<Listener>() };
    lists.push(entry);
    return {
      get matches() {
        return matcher(query);
      },
      media: query,
      onchange: null,
      addEventListener: (_type: string, listener: Listener) => entry.listeners.add(listener),
      removeEventListener: (_type: string, listener: Listener) => entry.listeners.delete(listener),
      addListener: (listener: Listener) => entry.listeners.add(listener),
      removeListener: (listener: Listener) => entry.listeners.delete(listener),
      dispatchEvent: () => false,
    } as unknown as MediaQueryList;
  };
}
