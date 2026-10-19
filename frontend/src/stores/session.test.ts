import { act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { wsClient } from '@/api/ws';
import { FakeWebSocket } from '@/test/fakeWebSocket';
import { FakeWsClient } from '@/test/fakeWsClient';
import { CID as C, PID } from '@/test/fakeMessageServer';
import { useAuth } from './auth';
import { chatKey, createChatStore, emptyChat, useChatStore } from './chat';
import { useComposer } from './composer';
import { useProjects } from './projects';
import { installSessionReset } from './session';

const project = {
  id: PID,
  name: 'demo',
  relPath: 'demo/demo.kicad_pro',
  hasSch: true,
  hasPcb: true,
  locked: false,
  readOnly: false,
  lockFiles: [],
  kicadVersion: '10.99.0',
  viewFiles: { sch: [], pcb: [] },
    viewBytes: {},
};

function signedInWithState() {
  useAuth.setState({ status: 'authenticated', username: 'admin', error: null });
  useChatStore.setState({ chats: { [chatKey(PID, C)]: { ...emptyChat(), loaded: true, lastSeenId: 7 } } });
  useComposer.getState().setText('half-written draft');
  useProjects.setState({ projects: [project], current: project });
}

function expectReset() {
  expect(useChatStore.getState().chats).toEqual({});
  expect(useComposer.getState().text).toBe('');
  expect(useProjects.getState()).toMatchObject({ projects: [], current: null });
}

let uninstall: () => void = () => {};
beforeEach(() => {
  uninstall = installSessionReset();
  FakeWebSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeWebSocket);
});
afterEach(() => {
  uninstall();
  wsClient.close();
});

describe('session reset when the auth store goes anonymous', () => {
  it('on logout', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 204 })));
    signedInWithState();
    await useAuth.getState().logout();
    expectReset();
  });

  it('on a WebSocket 4401 close', () => {
    signedInWithState();
    wsClient.ensure();
    const ws = FakeWebSocket.latest();
    act(() => {
      ws.serverOpen();
      ws.serverClose(4401);
    });
    expect(useAuth.getState().status).toBe('anonymous');
    expectReset();
  });

  it('on a login while already signed in (old socket closed too)', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) =>
      url === '/api/auth/me'
        ? new Response(JSON.stringify({ username: 'other' }), { headers: { 'content-type': 'application/json' } })
        : new Response(null, { status: 204 }),
    ));
    signedInWithState();
    wsClient.ensure();
    const old = FakeWebSocket.latest();
    old.serverOpen();
    expect(await useAuth.getState().login('other', 'pw')).toBe(true);
    expectReset();
    expect(old.readyState).toBe(FakeWebSocket.CLOSED);
    expect(wsClient.isOpen()).toBe(false);
    expect(useAuth.getState()).toMatchObject({ status: 'authenticated', username: 'other' });
  });

  it('not while staying signed in', () => {
    signedInWithState();
    useAuth.setState({ username: 'admin2' });
    expect(useComposer.getState().text).toBe('half-written draft');
    expect(useProjects.getState().current?.id).toBe(PID);
  });
});

describe('chat store reset()', () => {
  it('discards a load that was in flight and lets the project open again', async () => {
    const ws = new FakeWsClient();
    ws.open = true;
    const pages: Array<(rows: { messages: never[]; hasMore: boolean }) => void> = [];
    const store = createChatStore({
      ws,
      fetchPage: () => new Promise((resolve) => pages.push(resolve)),
    });
    const first = store.getState().open(PID, C);
    store.getState().reset();
    pages[0]?.({ messages: [], hasMore: false });
    await first;
    expect(store.getState().chats).toEqual({});
    expect(ws.framesOfType('subscribe')).toEqual([]);
    // a fresh open after reset issues a new request instead of reusing the stale one
    const second = store.getState().open(PID, C);
    expect(pages).toHaveLength(2);
    pages[1]?.({ messages: [], hasMore: false });
    await second;
    expect(store.getState().chats[chatKey(PID, C)]?.loaded).toBe(true);
    expect(ws.framesOfType('subscribe')).toHaveLength(1);
  });
});
