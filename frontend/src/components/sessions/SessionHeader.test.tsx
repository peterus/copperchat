import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/api/http';
import { conv } from '@/test/conv';
import { PID } from '@/test/fakeMessageServer';
import { fakeConversations, renderWithSessions } from '@/test/sessions';
import { SessionHeader } from './SessionHeader';

vi.mock('@/api/docs', () => ({ getDocIndex: vi.fn(async () => ({ root: 'docs/design', docs: [], ignored: [] })) }));

const C1 = '11111111111111111111111111111111';
const C2 = '22222222222222222222222222222222';
let f: ReturnType<typeof fakeConversations>;

beforeEach(async () => {
  f = fakeConversations([conv(C1, 1, { type: 'requirements' }), conv(C2, 2, { title: 'Power stage', type: 'schematic' })]);
  await f.store.getState().load(PID);
});

function Header({ cid = C1 }: { cid?: string }) {
  return <SessionHeader projectId={PID} conversationId={cid} />;
}

describe('SessionHeader', () => {
  it('shows the type icon, the title and the status chip', () => {
    renderWithSessions(<Header />, f.store);
    expect(screen.getByRole('img', { name: 'Requirements session' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Session 1' })).toBeInTheDocument();
    expect(screen.getByLabelText('Session status')).toHaveTextContent('open');
  });

  it('renames inline (trimmed, single line, 1–120 chars); Escape cancels', async () => {
    renderWithSessions(<Header />, f.store);
    fireEvent.click(screen.getByRole('button', { name: 'Rename session' }));
    const box = screen.getByRole('textbox', { name: 'Session title' });
    fireEvent.change(box, { target: { value: '   ' } });
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(screen.getByRole('alert')).toHaveTextContent('1–120 characters');
    fireEvent.change(box, { target: { value: 'x'.repeat(121) } });
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(f.api.patch).not.toHaveBeenCalled();
    fireEvent.keyDown(box, { key: 'Escape' });
    expect(screen.queryByRole('textbox', { name: 'Session title' })).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Rename session' }));
    const again = screen.getByRole('textbox', { name: 'Session title' });
    fireEvent.change(again, { target: { value: '  Interfaces\tand  power  ' } });
    fireEvent.keyDown(again, { key: 'Enter' });
    await waitFor(() => expect(f.api.patch).toHaveBeenCalledWith(PID, C1, { title: 'Interfaces and power' }));
    expect(await screen.findByRole('heading', { name: 'Interfaces and power' })).toBeInTheDocument();
  });

  it('shows "<other session> running" with a Stop that interrupts the running session', () => {
    renderWithSessions(<Header />, f.store);
    expect(screen.queryByText(/running/)).toBeNull();
    act(() => f.ws.emit({ type: 'run-started', projectId: PID, conversationId: C2, runId: 'r9' }));
    expect(screen.getByText('Power stage running')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Stop Power stage' }));
    expect(f.ws.framesOfType('interrupt')).toEqual([{ type: 'interrupt', projectId: PID, conversationId: C2, runId: 'r9' }]);
  });

  it('no running chip for the run of the session shown (its Stop is in the composer)', () => {
    renderWithSessions(<Header />, f.store);
    act(() => f.ws.emit({ type: 'run-started', projectId: PID, conversationId: C1, runId: 'r1' }));
    expect(screen.queryByText(/running/)).toBeNull();
  });

  it('Close session posts close and shows closing', async () => {
    renderWithSessions(<Header />, f.store);
    fireEvent.click(screen.getByRole('button', { name: 'Close session' }));
    await waitFor(() => expect(f.api.close).toHaveBeenCalledWith(PID, C1));
    expect(await screen.findByLabelText('Session status')).toHaveTextContent('closing');
    expect(screen.queryByRole('button', { name: 'Close session' })).toBeNull();
  });

  it('a refused close shows the server message', async () => {
    f.api.close.mockRejectedValueOnce(new ApiError(409, { error: { code: 'plan_open', message: 'Finish or abort the plan first' } }, 'Finish or abort the plan first'));
    renderWithSessions(<Header />, f.store);
    fireEvent.click(screen.getByRole('button', { name: 'Close session' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Finish or abort the plan first');
    expect(screen.getByLabelText('Session status')).toHaveTextContent('open');
  });

  it('▾ opens the session list; New session opens the dialog', async () => {
    renderWithSessions(<Header />, f.store);
    fireEvent.click(screen.getByRole('button', { name: 'Sessions' }));
    expect(screen.getByRole('list', { name: 'Sessions' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'New session' }));
    expect(await screen.findByRole('dialog', { name: 'New session' })).toBeInTheDocument();
  });
});
