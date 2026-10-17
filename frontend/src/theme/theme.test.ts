import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

// String form on purpose: jsdom's global URL class is rejected by Node's fileURLToPath.
const css = readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), 'theme.css'), 'utf8');

function tokensOf(selector: string): string[] {
  const start = css.indexOf(selector);
  expect(start, `selector ${selector} missing`).toBeGreaterThanOrEqual(0);
  const body = css.slice(css.indexOf('{', start) + 1, css.indexOf('}', start));
  return [...body.matchAll(/(--cc-[a-z0-9-]+)\s*:/g)].map((m) => m[1] ?? '').sort();
}

describe('theme tokens', () => {
  it('light and dark define the same token set', () => {
    const light = tokensOf(':root[data-theme="light"]');
    const dark = tokensOf(':root[data-theme="dark"]');
    expect(light.length).toBeGreaterThanOrEqual(10);
    expect(dark).toEqual(light);
  });

  it('every token is exposed to Tailwind via @theme inline', () => {
    for (const token of tokensOf(':root[data-theme="light"]')) {
      const name = token.replace('--cc-', '');
      expect(css).toContain(`--color-${name}: var(${token})`);
    }
  });
});
