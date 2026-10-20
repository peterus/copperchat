import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const http = vi.hoisted(() => ({ apiPost: vi.fn() }));
vi.mock('@/api/http', async (importOriginal) => ({ ...(await importOriginal<object>()), ...http }));

import { useUi } from '@/stores/ui';
import { conflict409, DOC_PID, docRead, REQ } from '@/test/docs';
import { DocView, type DocViewProps } from './DocView';

const base = `/projects/${DOC_PID}/docs/${REQ}`;

function renderView(over: Partial<DocViewProps> = {}) {
  const props: DocViewProps = {
    projectId: DOC_PID,
    doc: docRead(),
    onEdit: vi.fn(),
    onHistory: vi.fn(),
    reapproveVersion: null,
    onReapproveDone: vi.fn(),
    onChanged: vi.fn(),
    ...over,
  };
  render(<DocView {...props} />);
  return props;
}

beforeEach(() => {
  vi.resetAllMocks();
  http.apiPost.mockResolvedValue({ version: 'v1' });
  useUi.setState({ panelRequest: null });
});

describe('DocView', () => {
  it('renders the body through Markdown; Approve sends the shown version', async () => {
    const props = renderView();
    expect(screen.getByRole('heading', { name: 'Requirements' })).toBeInTheDocument();
    expect(screen.getByText('draft')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Approve' }));
    expect(http.apiPost).toHaveBeenCalledWith(`${base}/approve`, { version: 'v1' });
    await waitFor(() => expect(props.onChanged).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: 'Unapprove' })).toBeNull();
  });

  it('a 409 on Approve shows "changed — reload"', async () => {
    http.apiPost.mockRejectedValue(conflict409());
    const props = renderView();
    await userEvent.click(screen.getByRole('button', { name: 'Approve' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/changed — reload/i);
    expect(props.onChanged).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Reload' }));
    expect(props.onChanged).toHaveBeenCalled();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('an approved doc offers Unapprove', async () => {
    renderView({ doc: docRead({ meta: { approved: true, status: 'approved', approvedVersion: 'v1' } }) });
    expect(screen.getByText('approved')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Unapprove' }));
    expect(http.apiPost).toHaveBeenCalledWith(`${base}/unapprove`, { version: 'v1' });
  });

  it('decisions.md has neither Approve nor Unapprove', () => {
    renderView({ doc: docRead({ path: 'docs/design/decisions.md' }) });
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Unapprove' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument();
  });

  it('Let the agent rewrite clears user_edited with the shown version', async () => {
    const props = renderView({ doc: docRead({ meta: { userEdited: true } }) });
    expect(screen.getByText('edited by you')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Let the agent rewrite' }));
    expect(http.apiPost).toHaveBeenCalledWith(`${base}/clear-user-edited`, { version: 'v1' });
    await waitFor(() => expect(props.onChanged).toHaveBeenCalled());
  });

  it('no Let the agent rewrite while the doc is not user-edited', () => {
    renderView();
    expect(screen.queryByRole('button', { name: 'Let the agent rewrite' })).toBeNull();
  });

  it('a missing doc shows its template and offers Create, not Approve', async () => {
    const props = renderView({ doc: docRead({ exists: false, version: null, body: '', template: '# Template\n' }) });
    expect(screen.getByRole('heading', { name: 'Template' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Create' }));
    expect(props.onEdit).toHaveBeenCalled();
  });

  it('Approve again after saving an approved doc approves the saved version', async () => {
    const props = renderView({ reapproveVersion: 'v7' });
    expect(screen.getByRole('status')).toHaveTextContent('approve again?');
    await userEvent.click(screen.getByRole('button', { name: 'Approve again' }));
    expect(http.apiPost).toHaveBeenCalledWith(`${base}/approve`, { version: 'v7' });
    await waitFor(() => expect(props.onReapproveDone).toHaveBeenCalled());
  });

  it('a parts notes link opens the Parts panel; other relative links are plain text', async () => {
    renderView({ doc: docRead({ body: '[LM317](../parts/LM317/notes.md) and [arch](architecture.md)' }) });
    await userEvent.click(screen.getByRole('button', { name: 'LM317' }));
    expect(useUi.getState().panelRequest).toEqual({ id: 'parts', params: { mpn: 'LM317' } });
    expect(screen.getByText('arch').closest('a, button')).toBeNull();
  });
});
