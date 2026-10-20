import { fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { DocIndexDTO, DocIndexEntry } from '@/api/docs';
import { getDocIndex } from '@/api/docs';
import { registerPanel } from '@/layouts/panels';
import { useUi } from '@/stores/ui';
import { conv } from '@/test/conv';
import { PID } from '@/test/fakeMessageServer';
import { fakeConversations, renderWithSessions } from '@/test/sessions';
import { NewSessionDialog } from './NewSessionDialog';

vi.mock('@/api/docs', () => ({ getDocIndex: vi.fn() }));
const index = vi.mocked(getDocIndex);

const REQ = 'docs/design/requirements.md';
const entry = (extra: Partial<DocIndexEntry>): DocIndexEntry => ({
  path: REQ,
  fixed: true,
  exists: true,
  approved: false,
  changedSinceApproval: false,
  approvedVersion: null,
  approvedAt: null,
  userEdited: false,
  version: 'v0123456789abcdef',
  bytes: 100,
  updatedAt: null,
  headings: [],
  openPoints: 0,
  lastAuthor: null,
  lastConversationTitle: null,
  ...extra,
});
const docs = (...entries: DocIndexEntry[]): DocIndexDTO => ({ root: 'docs/design', docs: entries, ignored: [] });

let f: ReturnType<typeof fakeConversations>;
let onClose: ReturnType<typeof vi.fn<() => void>>;
let unregister: (() => void) | null = null;

beforeEach(async () => {
  f = fakeConversations([conv('11111111111111111111111111111111', 1)]);
  await f.store.getState().load(PID);
  onClose = vi.fn<() => void>();
  useUi.getState().clearPanelRequest();
});
afterEach(() => {
  unregister?.();
  unregister = null;
});

function renderDialog() {
  return renderWithSessions(<NewSessionDialog projectId={PID} onClose={onClose} />, f.store, `/p/${PID}`);
}

const card = (name: RegExp) => screen.getByRole('radio', { name });

describe('NewSessionDialog', () => {
  it('offers Requirements, Schematic and Free; Board is greyed out', async () => {
    index.mockResolvedValue(docs(entry({ exists: false })));
    renderDialog();
    expect(await screen.findByRole('dialog', { name: 'New session' })).toBeInTheDocument();
    await waitFor(() => expect(card(/^Requirements/)).toBeChecked());
    expect(card(/^Schematic/)).toBeEnabled();
    expect(card(/^Free/)).toBeEnabled();
    expect(card(/^Board/)).toBeDisabled();
    expect(screen.getByText(/coming with board editing/i)).toBeInTheDocument();
  });

  it('defaults to Schematic when requirements.md exists', async () => {
    index.mockResolvedValue(docs(entry({ approved: true })));
    renderDialog();
    await waitFor(() => expect(card(/^Schematic/)).toBeChecked());
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByRole('button', { name: 'Start session' })).toBeEnabled();
  });

  it('warns for Schematic while requirements are draft; Start anyway creates it', async () => {
    index.mockResolvedValue(docs(entry({ openPoints: 2 })));
    renderDialog();
    await waitFor(() => expect(card(/^Schematic/)).toBeChecked());
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Requirements are not approved (draft, 2 open points). The agent will work from unapproved requirements',
    );
    fireEvent.change(screen.getByRole('textbox', { name: /Title/ }), { target: { value: '  Power  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Start anyway' }));
    await waitFor(() => expect(f.api.create).toHaveBeenCalledWith(PID, { type: 'schematic', title: 'Power' }));
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(new RegExp(`/p/${PID}/s/[0-9a-f]{32}$`)));
    expect(onClose).toHaveBeenCalled();
  });

  it('warns when requirements are missing or changed since approval; no warning for Free', async () => {
    index.mockResolvedValue(docs(entry({ exists: false })));
    const { unmount } = renderDialog();
    await waitFor(() => expect(card(/^Requirements/)).toBeChecked());
    expect(screen.queryByRole('alert')).toBeNull();
    fireEvent.click(card(/^Schematic/));
    expect(screen.getByRole('alert')).toHaveTextContent('Requirements are missing');
    fireEvent.click(card(/^Free/));
    expect(screen.queryByRole('alert')).toBeNull();
    unmount();

    index.mockResolvedValue(docs(entry({ approved: true, changedSinceApproval: true })));
    renderDialog();
    await waitFor(() => expect(card(/^Schematic/)).toBeChecked());
    expect(screen.getByRole('alert')).toHaveTextContent('changed since approval');
  });

  it('Open requirements opens the Docs panel when it is registered', async () => {
    index.mockResolvedValue(docs(entry({})));
    const { unmount } = renderDialog();
    await waitFor(() => expect(card(/^Schematic/)).toBeChecked());
    // without a Docs panel the button is not offered
    expect(screen.queryByRole('button', { name: 'Open requirements' })).toBeNull();
    unmount();

    unregister = registerPanel({ id: 'docs', title: 'Docs', icon: () => null, component: () => null });
    renderDialog();
    await waitFor(() => expect(card(/^Schematic/)).toBeChecked());
    fireEvent.click(screen.getByRole('button', { name: 'Open requirements' }));
    expect(useUi.getState().panelRequest).toEqual({ id: 'docs', params: { path: REQ } });
    expect(onClose).toHaveBeenCalled();
    expect(f.api.create).not.toHaveBeenCalled();
  });

  it('a failed docs check defaults to Requirements and says so for Schematic', async () => {
    index.mockRejectedValue(new Error('offline'));
    renderDialog();
    await waitFor(() => expect(card(/^Requirements/)).toBeChecked());
    fireEvent.click(card(/^Schematic/));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not check the requirements');
  });

  it('a refused create shows the error and keeps the dialog', async () => {
    index.mockResolvedValue(docs(entry({ exists: false })));
    f.api.create.mockRejectedValueOnce(new Error('Title must be one line'));
    renderDialog();
    await waitFor(() => expect(card(/^Requirements/)).toBeChecked());
    fireEvent.click(screen.getByRole('button', { name: 'Start session' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Title must be one line');
    expect(onClose).not.toHaveBeenCalled();
  });

  it('Cancel and Escape close without creating', async () => {
    index.mockResolvedValue(docs());
    renderDialog();
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(2);
    expect(f.api.create).not.toHaveBeenCalled();
  });
});
