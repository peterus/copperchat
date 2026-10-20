import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const http = vi.hoisted(() => ({ apiGet: vi.fn(), apiPost: vi.fn() }));
vi.mock('@/api/http', async (importOriginal) => ({ ...(await importOriginal<object>()), ...http }));

import { createDocsStore, DocsStoreContext } from '@/stores/docs';
import { conflict409, DOC_PID, REQ, revision } from '@/test/docs';
import { FakeWsClient } from '@/test/fakeWsClient';
import { DocHistory, type DocHistoryProps } from './DocHistory';

const url = `/projects/${DOC_PID}/docs/${REQ}`;
const CONTENT = {
  content: '---\ndoc: requirements\nstatus: draft\n---\n# R\n\nnew line\n',
  prevContent: '---\ndoc: requirements\nstatus: approved\n---\n# R\n\nold line\n',
};

function Harness(props: Omit<DocHistoryProps, 'selected' | 'onSelect'> & { initial: number | null }) {
  const [selected, setSelected] = useState<number | null>(props.initial);
  return <DocHistory {...props} selected={selected} onSelect={setSelected} />;
}

async function setup(initial: number | null = null) {
  const ws = new FakeWsClient();
  const store = createDocsStore(ws, { getIndex: vi.fn().mockResolvedValue({ root: 'docs/design', docs: [], ignored: [] }), getDoc: vi.fn() });
  await store.getState().load(DOC_PID);
  const props = { projectId: DOC_PID, path: REQ, currentVersion: 'v3', onReverted: vi.fn(), onRevertConflict: vi.fn() };
  render(
    <DocsStoreContext.Provider value={store}>
      <Harness {...props} initial={initial} />
    </DocsStoreContext.Provider>,
  );
  return { props, ws };
}

beforeEach(() => {
  vi.resetAllMocks();
  http.apiGet.mockImplementation(async (path: string) =>
    path.includes('/revisions?') ? [revision(1), revision(3, { author: 'user', conversationTitle: null }), revision(2)] : CONTENT,
  );
  http.apiPost.mockResolvedValue({ version: 'v4' });
});

describe('DocHistory', () => {
  it('lists revisions newest first with author, session, summary and +/−', async () => {
    await setup();
    const items = await screen.findAllByRole('listitem');
    expect(items.map((i) => i.textContent)).toEqual([
      expect.stringMatching(/^you\+3−1.*change 3$/),
      expect.stringMatching(/^agent · Session 2 — Requirements\+2−1.*change 2$/),
      expect.stringMatching(/^agent · Session 2 — Requirements\+1−1.*change 1$/),
    ]);
    expect(http.apiGet).toHaveBeenCalledWith(`${url}/revisions?limit=50`);
  });

  it('a revision shows the diff against its predecessor, without the front matter', async () => {
    await setup();
    await userEvent.click((await screen.findAllByRole('listitem'))[0]!.querySelector('button')!);
    const rows = (await screen.findAllByRole('row')).map((r) => r.textContent);
    expect(http.apiGet).toHaveBeenCalledWith(`${url}/revisions/3`);
    expect(rows).toContain('- old line');
    expect(rows).toContain('+ new line');
    expect(rows.join('\n')).not.toMatch(/status/);
    await userEvent.click(screen.getByRole('button', { name: '← All revisions' }));
    expect(await screen.findAllByRole('listitem')).toHaveLength(3);
  });

  it('opens a given revision directly (View diff)', async () => {
    await setup(2);
    await waitFor(() => expect(screen.getAllByRole('row').map((r) => r.textContent)).toContain('+ new line'));
    expect(http.apiGet).toHaveBeenCalledWith(`${url}/revisions/2`);
  });

  it('Revert to this posts the revert with the current version', async () => {
    const { props } = await setup(2);
    await userEvent.click(await screen.findByRole('button', { name: 'Revert to this' }));
    expect(http.apiPost).toHaveBeenCalledWith(`${url}/revisions/2/revert`, { baseVersion: 'v3' });
    await waitFor(() => expect(props.onReverted).toHaveBeenCalled());
  });

  it('a 409 on revert continues in the conflict flow with the revision text', async () => {
    http.apiPost.mockRejectedValue(conflict409());
    const { props } = await setup(2);
    await userEvent.click(await screen.findByRole('button', { name: 'Revert to this' }));
    await waitFor(() =>
      expect(props.onRevertConflict).toHaveBeenCalledWith('# R\n\nnew line\n', {
        currentVersion: 'v2',
        author: 'agent',
        conversationTitle: 'Session 3 — Schematic',
      }),
    );
    expect(props.onReverted).not.toHaveBeenCalled();
  });

  it('a doc-updated frame reloads the list', async () => {
    const { ws } = await setup();
    await screen.findAllByRole('listitem');
    act(() =>
      ws.emit({ type: 'doc-updated', projectId: DOC_PID, path: REQ, version: 'v9', status: 'draft', userEdited: false, revisionId: 9, author: 'agent' }),
    );
    await waitFor(() => expect(http.apiGet.mock.calls.filter(([p]) => String(p).includes('/revisions?'))).toHaveLength(2));
  });
});
