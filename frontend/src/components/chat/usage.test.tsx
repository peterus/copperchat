import { act, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { RateLimitBanner } from './RateLimitBanner';
import { UsageIndicator, formatTokens } from './UsageIndicator';

afterEach(() => vi.useRealTimers());

describe('usage', () => {
  it('formats token counts', () => {
    expect([999, 1234, 15_000, 999_999, 2_500_000].map(formatTokens)).toEqual(['999', '1.2k', '15k', '1M', '2.5M']);
  });

  it('shows input incl. cache, output and cost', () => {
    render(<UsageIndicator usage={{ inputTokens: 1000, outputTokens: 250, cacheReadTokens: 9000, cacheCreationTokens: 200, costUsd: 0.1234 }} />);
    expect(screen.getByLabelText('Token usage')).toHaveTextContent('in 10k · out 250 · $0.12');
  });

  it('shows a rate-limit warning from the usage payload', () => {
    render(<UsageIndicator usage={{ inputTokens: 1, outputTokens: 1, cacheReadTokens: 0, cacheCreationTokens: 0, rateLimit: { status: 'allowed_warning', resetsAt: '2026-10-15T10:30:00Z' } }} />);
    expect(screen.getByLabelText('Token usage')).toHaveTextContent(/limit nearly reached, resets/);
  });

  it('renders nothing without usage', () => {
    const { container } = render(<UsageIndicator usage={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('rate-limit banner shows until the reset time passes', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-15T10:00:00Z'));
    const { rerender } = render(<RateLimitBanner until="2026-10-15T10:30:00Z" />);
    expect(screen.getByRole('status')).toHaveTextContent(/Rate limit reached.*resume automatically/);
    vi.setSystemTime(new Date('2026-10-15T10:31:00Z'));
    rerender(<RateLimitBanner until="2026-10-15T10:30:00Z" />);
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('rate-limit banner hides itself when the reset time passes without a rerender', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-15T10:29:50Z'));
    render(<RateLimitBanner until="2026-10-15T10:30:00Z" />);
    expect(screen.getByRole('status')).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(11_000));
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('rate-limit banner ignores missing or invalid times', () => {
    const { container, rerender } = render(<RateLimitBanner until={null} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<RateLimitBanner until="not a date" />);
    expect(container).toBeEmptyDOMElement();
  });
});
