import { act, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ChatStoreContext } from '@/hooks/useChat';
import { createChatStore, fetchMessagesPage, type ChatStore } from '@/stores/chat';
import { useComposer } from '@/stores/composer';
import { FakeMessageServer, PID, row } from '@/test/fakeMessageServer';
import { FakeWsClient } from '@/test/fakeWsClient';
import { ChatPanel } from './ChatPanel';

// Counts how often each Markdown text is parsed (react-markdown is the expensive part).
const parses = new Map<string, number>();
vi.mock('react-markdown', () => ({
  default: ({ children }: { children: string }) => {
    parses.set(children, (parses.get(children) ?? 0) + 1);
    return <p>{children}</p>;
  },
}));

let ws: FakeWsClient;
let store: ChatStore;

beforeEach(() => {
  parses.clear();
  vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} unobserve() {} takeRecords() { return []; } });
  useComposer.getState().clear();
  ws = new FakeWsClient();
  ws.open = true;
  new FakeMessageServer([
    row(1, { content: 'first answer' }),
    row(2, { kind: 'tool_use', content: 'research_part', metadata: { toolUseId: 'a1', name: 'research_part', input: { mpn: 'X1' } } }),
    row(3, { kind: 'tool_result', content: 'card summary', metadata: { toolUseId: 'a1', isError: false, images: [] } }),
    row(4, { content: 'sec', status: 'streaming' }),
  ]).install();
  store = createChatStore({ ws, fetchPage: fetchMessagesPage });
});

describe('ChatPanel rendering cost', () => {
  it('a streaming delta re-parses only the Markdown of the row it changes', async () => {
    render(<ChatStoreContext.Provider value={store}><ChatPanel projectId={PID} /></ChatStoreContext.Provider>);
    await screen.findByText('sec');
    const before = { first: parses.get('first answer'), summary: parses.get('card summary') };
    expect(before.first).toBeGreaterThan(0);
    expect(before.summary).toBeGreaterThan(0);
    act(() => ws.emit({ type: 'message-delta', projectId: PID, messageId: 4, contentDelta: 'ond' }));
    act(() => ws.emit({ type: 'message-delta', projectId: PID, messageId: 4, contentDelta: '!' }));
    expect(screen.getByText('second!')).toBeInTheDocument();
    expect(parses.get('first answer')).toBe(before.first);
    expect(parses.get('card summary')).toBe(before.summary);
  });
});
