import type { MessageDTO } from '@/protocol/frames';

/**
 * Tools whose calls run a sub-agent (R6, option B): only our MCP tool `research_part`. The main
 * agent has no built-in Agent/Task tool. Sub-agent rows carry `metadata.parentToolUseId` = the
 * delegation call's `toolUseId`; the call's own tool_result is the sub-agent's final summary.
 */
export const DELEGATION_TOOLS: ReadonlySet<string> = new Set(['research_part']);

export interface SubagentGroup {
  steps: MessageDTO[];
  result: MessageDTO | null;
}

const QUERY_TITLE_MAX = 40;

const str = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null);
const input = (m: MessageDTO): Record<string, unknown> =>
  m.metadata.input && typeof m.metadata.input === 'object' ? (m.metadata.input as Record<string, unknown>) : {};
/** MCP tools of other servers keep their `mcp__<server>__` prefix; ours arrive stripped. */
const bareName = (name: string): string => name.replace(/^mcp__[^_]+(?:_[^_]+)*__/, '');

export function isDelegation(m: MessageDTO): boolean {
  return (
    m.kind === 'tool_use' &&
    !str(m.metadata.parentToolUseId) &&
    DELEGATION_TOOLS.has(str(m.metadata.name) ?? '') &&
    !!str(m.metadata.toolUseId)
  );
}

/**
 * Splits sub-agent rows out of the flat history. Rows whose parent is not a loaded delegation call
 * (parent on an older page, or not a delegation at all) stay top-level: data is never dropped.
 */
export function groupSubagentRows(messages: MessageDTO[]): { topLevel: MessageDTO[]; groups: Map<string, SubagentGroup> } {
  const groups = new Map<string, SubagentGroup>();
  for (const m of messages) if (isDelegation(m)) groups.set(str(m.metadata.toolUseId)!, { steps: [], result: null });
  if (groups.size === 0) return { topLevel: messages, groups };
  const topLevel: MessageDTO[] = [];
  for (const m of messages) {
    const parent = str(m.metadata.parentToolUseId);
    const own = str(m.metadata.toolUseId);
    const group = parent ? groups.get(parent) : undefined;
    if (group) group.steps.push(m);
    else if (!parent && m.kind === 'tool_result' && own && groups.has(own)) groups.get(own)!.result = m;
    else topLevel.push(m);
  }
  return { topLevel, groups };
}

export function subagentTitle(toolUse: MessageDTO): string {
  const i = input(toolUse);
  const query = str(i.query)?.trim();
  const fromQuery = query
    ? query.length > QUERY_TITLE_MAX
      ? `${query.slice(0, QUERY_TITLE_MAX).trimEnd()}…`
      : query
    : null;
  return `Part research: ${str(i.mpn) ?? str(i.description) ?? fromQuery ?? 'part'}`;
}

/** One-line label for a sub-agent row, or null when the row is not worth a step line. */
export function describeStep(m: MessageDTO): string | null {
  if (m.kind === 'text') return m.content.trim().slice(0, 300) || null;
  if (m.kind === 'tool_result') return m.metadata.isError === true ? `Failed: ${m.content.slice(0, 160)}` : null;
  if (m.kind !== 'tool_use') return null;
  const i = input(m);
  const name = bareName(str(m.metadata.name) ?? (m.content || 'tool'));
  const path = str(i.file_path) ?? str(i.path) ?? '';
  switch (name) {
    case 'WebSearch':
      return `Searched: ${str(i.query) ?? ''}`;
    case 'WebFetch':
      return `Fetched: ${str(i.url) ?? ''}`;
    case 'Read':
      return `Read: ${path}`;
    case 'Glob':
    case 'Grep':
      return `Looked in: ${path || '.'}`;
    case 'Write':
    case 'Edit':
      if (path === 'notes.md' || path.endsWith('/notes.md')) return `Notes written: ${path}`;
      if (path.endsWith('.pdf')) return `Datasheet saved: ${path}`;
      return `Saved: ${path}`;
    default:
      if (/datasheet/i.test(name)) return `Datasheet: ${str(i.mpn) ?? str(i.url) ?? str(i.path) ?? ''}`.trimEnd();
      return name;
  }
}
