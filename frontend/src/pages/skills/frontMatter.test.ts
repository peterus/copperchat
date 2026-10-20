import { describe, expect, it } from 'vitest';
import { readSessions, writeSessions } from './frontMatter';

const fm = (...lines: string[]) => `---\n${lines.join('\n')}\n---\nbody`;

describe('skill front matter `sessions:`', () => {
  it('reads the comma-separated list like the backend (trimmed, lower-case, sorted)', () => {
    expect(readSessions(fm('name: a', 'sessions: Schematic , free'))).toEqual(['free', 'schematic']);
    expect(readSessions(fm('name: a', 'sessions: "requirements"'))).toEqual(['requirements']);
    expect(readSessions(fm('name: a', 'sessions:'))).toBeNull();
    expect(readSessions(fm('name: a'))).toBeNull();
    expect(readSessions('sessions: free\nno front matter')).toBeNull();
    expect(readSessions(`${fm('name: a')}\nsessions: free`)).toBeNull(); // body, not front matter
  });

  it('replaces the key in place and drops duplicates', () => {
    expect(writeSessions(fm('name: a', 'sessions: free', 'description: d', 'sessions: x'), ['schematic', 'free'])).toBe(
      fm('name: a', 'sessions: free, schematic', 'description: d'),
    );
  });

  it('appends the key when missing and removes it for no restriction', () => {
    expect(writeSessions(fm('name: a'), ['requirements'])).toBe(fm('name: a', 'sessions: requirements'));
    expect(writeSessions(fm('name: a', 'sessions: free', 'description: d'), [])).toBe(fm('name: a', 'description: d'));
    expect(writeSessions('plain', [])).toBe('plain');
  });

  it('keeps CRLF line endings', () => {
    const crlf = '---\r\nname: a\r\n---\r\nbody';
    expect(writeSessions(crlf, ['free'])).toBe('---\r\nname: a\r\nsessions: free\r\n---\r\nbody');
    expect(readSessions('---\r\nname: a\r\nsessions: free\r\n---\r\n')).toEqual(['free']);
  });
});
