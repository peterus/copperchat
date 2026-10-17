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
