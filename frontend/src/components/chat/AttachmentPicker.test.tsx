import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { setUnauthorizedHandler } from '@/api/http';
import { AttachmentPicker } from './AttachmentPicker';

const ID_A = 'a'.repeat(32);
const ID_B = 'b'.repeat(32);
const json = (body: unknown, status: number) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const png = (name: string) => new File([new Uint8Array([137, 80, 78, 71])], name, { type: 'image/png' });

afterEach(() => vi.unstubAllGlobals());

describe('AttachmentPicker', () => {
  it('uploads a picked file and reports the AttachmentRef', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ id: ID_A, kind: 'image', name: 'p.png' }, 201));
    vi.stubGlobal('fetch', fetchMock);
    const addAttachment = vi.fn();
    render(<AttachmentPicker disabled={false} projectId="p1" attachments={[]} addAttachment={addAttachment} />);
    const input = screen.getByLabelText(/attach file/i) as HTMLInputElement;
    expect(input.accept).toBe('image/*,application/pdf');
    expect(input.multiple).toBe(true);
    const photo = screen.getByLabelText(/take photo/i);
    expect(photo).toHaveAttribute('capture', 'environment');
    expect(photo).toHaveAttribute('accept', 'image/*');
    await userEvent.upload(input, png('p.png'));
    await waitFor(() => expect(addAttachment).toHaveBeenCalledWith({ id: ID_A, kind: 'image', name: 'p.png' }));
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/projects/p1/uploads');
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('same-origin');
    expect((init.body as FormData).get('file')).toBeInstanceOf(File);
  });

  it('adds several files one by one', async () => {
    const existing = { id: 'c'.repeat(32), kind: 'pdf' as const, name: 'old.pdf' };
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(json({ id: ID_A, kind: 'image', name: 'a.png' }, 201))
        .mockResolvedValueOnce(json({ id: ID_B, kind: 'image', name: 'b.png' }, 201)),
    );
    const addAttachment = vi.fn();
    render(<AttachmentPicker disabled={false} projectId="p1" attachments={[existing]} addAttachment={addAttachment} />);
    await userEvent.upload(screen.getByLabelText(/attach file/i), [png('a.png'), png('b.png')]);
    await waitFor(() => expect(addAttachment).toHaveBeenCalledTimes(2));
    expect(addAttachment.mock.calls).toEqual([
      [{ id: ID_A, kind: 'image', name: 'a.png' }],
      [{ id: ID_B, kind: 'image', name: 'b.png' }],
    ]);
  });

  it('shows the server error on rejection', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ detail: 'only PNG, JPEG, WebP or PDF' }, 415)));
    render(<AttachmentPicker disabled={false} projectId="p1" attachments={[]} addAttachment={vi.fn()} />);
    await userEvent.upload(screen.getByLabelText(/attach file/i), new File(['x'], 'x.heic', { type: 'image/heic' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('only PNG');
  });

  it('falls back to the status when the error has no detail', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('oops', { status: 502 })));
    render(<AttachmentPicker disabled={false} projectId="p1" attachments={[]} addAttachment={vi.fn()} />);
    await userEvent.upload(screen.getByLabelText(/attach file/i), png('p.png'));
    expect(await screen.findByRole('alert')).toHaveTextContent('Upload failed (502)');
  });

  it('is disabled while the composer is disabled', () => {
    render(<AttachmentPicker disabled projectId="p1" attachments={[]} addAttachment={vi.fn()} />);
    expect(screen.getByLabelText(/attach file/i)).toBeDisabled();
    expect(screen.getByLabelText(/take photo/i)).toBeDisabled();
  });

  it('reports a 401 to the shared unauthorized handler', async () => {
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ detail: 'not logged in' }, 401)));
    render(<AttachmentPicker disabled={false} projectId="p1" attachments={[]} addAttachment={vi.fn()} />);
    await userEvent.upload(screen.getByLabelText(/attach file/i), png('p.png'));
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
    setUnauthorizedHandler(null);
  });
});
