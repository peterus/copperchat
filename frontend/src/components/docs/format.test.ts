import { describe, expect, it } from 'vitest';
import { docBody, partOfLink } from './format';

describe('docBody (mirrors the backend front-matter split)', () => {
  it('drops the front matter block', () => {
    expect(docBody('---\ndoc: r\nstatus: draft\n---\n# R\n')).toBe('# R\n');
  });
  it('handles a BOM, CRLF and an indented delimiter', () => {
    expect(docBody('﻿---\r\ndoc: r\r\n  ---  \r\n# R\r\n')).toBe('# R\r\n');
  });
  it('an empty block and a block at the very end', () => {
    expect(docBody('---\n---\nbody')).toBe('body');
    expect(docBody('---\ndoc: r\n---')).toBe('');
  });
  it('without or with an unterminated block the whole text is body', () => {
    expect(docBody('# R\n---\n')).toBe('# R\n---\n');
    expect(docBody('---\ndoc: r\n# R\n')).toBe('---\ndoc: r\n# R\n');
  });
});

describe('partOfLink', () => {
  it('takes exactly ../parts/<MPN>/notes.md', () => {
    expect(partOfLink('../parts/LM317/notes.md')).toBe('LM317');
    expect(partOfLink('../parts/TPS%2062133/notes.md')).toBe('TPS 62133');
    expect(partOfLink('../parts/%2E%2E/notes.md')).toBeNull();
    expect(partOfLink('../parts/a%2Fb/notes.md')).toBeNull();
    expect(partOfLink('../parts/%E0%A4%A/notes.md')).toBeNull();
    expect(partOfLink('parts/LM317/notes.md')).toBeNull();
    expect(partOfLink('../parts/LM317/datasheet.pdf')).toBeNull();
  });
});
