/**
 * The `sessions:` key of a SKILL.md front matter (spec §8.4). The backend parser is flat
 * `key: value` (skills/store.py `parse_front_matter`), so the value is comma-separated:
 * `sessions: requirements, schematic`. No key = the skill applies to every session type.
 */

const SESSIONS_KEY = /^sessions\s*:/;

/** Index of the closing `---` line, or -1 when the text has no front matter. */
function frontMatterEnd(lines: string[]): number {
  if (lines[0]?.trim() !== '---') return -1;
  return lines.findIndex((l, i) => i > 0 && l.trim() === '---');
}

/** Session types listed in the front matter (as the backend reads them); null = every type. */
export function readSessions(content: string): string[] | null {
  const lines = content.split('\n');
  const end = frontMatterEnd(lines);
  let value: string | null = null;
  for (const line of lines.slice(1, Math.max(end, 1))) {
    if (SESSIONS_KEY.test(line)) value = line.slice(line.indexOf(':') + 1).trim().replace(/^["']|["']$/g, '');
  }
  const types = [...new Set((value ?? '').split(',').map((t) => t.trim().toLowerCase()).filter(Boolean))];
  return types.length > 0 ? types.sort() : null;
}

/** `content` with `sessions:` set to `types` (comma-separated), or removed when `types` is empty. */
export function writeSessions(content: string, types: readonly string[]): string {
  const lines = content.split('\n');
  const end = frontMatterEnd(lines);
  const entry = types.length > 0 ? `sessions: ${[...types].sort().join(', ')}` : null;
  if (end < 0) return entry ? `---\n${entry}\n---\n${content}` : content;
  const eol = lines[0]!.endsWith('\r') ? '\r' : '';
  // Replace the key in place (dropping duplicates), or append it to the front matter.
  const head: string[] = [];
  let placed = entry === null;
  for (const line of lines.slice(1, end)) {
    if (!SESSIONS_KEY.test(line)) head.push(line);
    else if (!placed) {
      head.push(entry + eol);
      placed = true;
    }
  }
  if (!placed) head.push(entry + eol);
  return [lines[0]!, ...head, ...lines.slice(end)].join('\n');
}
