import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { row } from '@/test/fakeMessageServer';
import { SubagentCard } from './SubagentCard';
import { DELEGATION_TOOLS, describeStep, groupSubagentRows, isDelegation, subagentTitle } from './subagent';

const researchInput = { mpn: 'TPS62133', query: 'How is it laid out?' };
const call = row(10, { kind: 'tool_use', content: 'research_part', metadata: { toolUseId: 'a1', name: 'research_part', input: researchInput } });
const sub = (id: number, md: Record<string, unknown>) =>
  row(id, { kind: 'tool_use', metadata: { parentToolUseId: 'a1', ...md } });
const steps = [
  row(11, { kind: 'text', content: 'Looking for the datasheet', metadata: { parentToolUseId: 'a1' } }),
  sub(12, { toolUseId: 'w1', name: 'WebSearch', input: { query: 'TPS62133 datasheet' } }),
  sub(13, { toolUseId: 'f1', name: 'WebFetch', input: { url: 'https://ti.com/x.pdf', prompt: 'layout' } }),
  sub(14, { toolUseId: 'd1', name: 'download_datasheet', input: { mpn: 'TPS62133', url: 'https://ti.com/x.pdf' } }),
  sub(15, { toolUseId: 'n1', name: 'Write', input: { file_path: 'docs/parts/TPS62133/notes.md', content: '…' } }),
  row(16, { kind: 'tool_result', content: 'ok', metadata: { parentToolUseId: 'a1', toolUseId: 'n1', isError: false, images: [] } }),
];
const result = row(17, { kind: 'tool_result', content: '**3 MHz buck**, notes in docs/parts/TPS62133/notes.md', metadata: { toolUseId: 'a1', isError: false, images: [] } });

describe('isDelegation', () => {
  it('only our research_part MCP tool delegates (no built-in Agent tool)', () => {
    expect([...DELEGATION_TOOLS]).toEqual(['research_part']);
    expect(isDelegation(call)).toBe(true);
    expect(isDelegation(row(1, { kind: 'tool_use', metadata: { toolUseId: 'x', name: 'Agent', input: {} } }))).toBe(false);
    expect(isDelegation(row(2, { kind: 'tool_result', metadata: { toolUseId: 'a1', name: 'research_part' } }))).toBe(false);
  });
});

describe('groupSubagentRows', () => {
  it('moves sub-agent rows and the delegation result under the research_part call', () => {
    const before = row(9, { content: 'Let me check.' });
    const after = row(18, { content: 'Done.' });
    const { topLevel, groups } = groupSubagentRows([before, call, ...steps, result, after]);
    expect(topLevel.map((m) => m.id)).toEqual([9, 10, 18]);
    expect(groups.get('a1')?.steps.map((m) => m.id)).toEqual([11, 12, 13, 14, 15, 16]);
    expect(groups.get('a1')?.result?.id).toBe(17);
  });

  it('keeps orphans (parent call on an older page) at top level', () => {
    const { topLevel, groups } = groupSubagentRows([steps[1]!]);
    expect(topLevel.map((m) => m.id)).toEqual([12]);
    expect(groups.size).toBe(0);
  });

  it('never drops rows whose parent is not a known delegation', () => {
    const other = row(20, { kind: 'tool_use', content: 'pcb_get_board_summary', metadata: { toolUseId: 'o1', name: 'pcb_get_board_summary', input: {} } });
    const child = row(21, { kind: 'text', content: 'stray', metadata: { parentToolUseId: 'o1' } });
    const unknown = row(22, { kind: 'text', content: 'lost', metadata: { parentToolUseId: 'zz' } });
    const { topLevel } = groupSubagentRows([call, other, child, unknown]);
    expect(topLevel.map((m) => m.id)).toEqual([10, 20, 21, 22]);
  });
});

describe('describeStep / subagentTitle', () => {
  it('titles the card from mpn, then description, then the truncated query', () => {
    expect(subagentTitle(call)).toBe('Part research: TPS62133');
    const t = (input: Record<string, unknown>) => subagentTitle(row(1, { kind: 'tool_use', metadata: { name: 'research_part', input } }));
    expect(t({ description: 'LM317' })).toBe('Part research: LM317');
    expect(t({ query: 'Which 3.3 V LDO with low quiescent current fits the sensor board?' }))
      .toBe('Part research: Which 3.3 V LDO with low quiescent curre…');
    expect(t({ query: 'short one' })).toBe('Part research: short one');
    expect(t({})).toBe('Part research: part');
  });

  it('labels researcher steps', () => {
    expect(steps.map(describeStep)).toEqual([
      'Looking for the datasheet',
      'Searched: TPS62133 datasheet',
      'Fetched: https://ti.com/x.pdf',
      'Datasheet: TPS62133',
      'Notes written: docs/parts/TPS62133/notes.md',
      null,
    ]);
    expect(describeStep(sub(30, { name: 'Write', input: { file_path: 'docs/parts/X/pinout.md' } }))).toBe('Saved: docs/parts/X/pinout.md');
    expect(describeStep(sub(31, { name: 'mcp__other__lookup', input: {} }))).toBe('lookup');
    expect(describeStep(row(2, { kind: 'tool_result', content: 'denied: outside docs/parts', metadata: { parentToolUseId: 'a1', isError: true } })))
      .toBe('Failed: denied: outside docs/parts');
  });
});

describe('SubagentCard', () => {
  it('is open with live steps while running, then collapsed with the summary', () => {
    const { rerender } = render(<SubagentCard toolUse={call} group={{ steps: steps.slice(0, 2), result: null }} runActive />);
    const summary = screen.getByText('Part research: TPS62133');
    expect(summary.closest('details')).toHaveAttribute('open');
    expect(screen.getByText('running')).toBeInTheDocument();
    expect(screen.getByText('Searched: TPS62133 datasheet')).toBeInTheDocument();

    rerender(<SubagentCard toolUse={call} group={{ steps, result }} runActive />);
    expect(screen.getByText('done')).toBeInTheDocument();
    expect(screen.getByText('Part research: TPS62133').closest('details')).not.toHaveAttribute('open');
    fireEvent.click(screen.getByText('Part research: TPS62133'));
    expect(screen.getByText('Part research: TPS62133').closest('details')).toHaveAttribute('open');
    expect(screen.getByText('3 MHz buck').tagName).toBe('STRONG');
    expect(screen.getByText('Notes written: docs/parts/TPS62133/notes.md')).toBeInTheDocument();
  });

  it('shows "failed" when the delegation result is an error', () => {
    const failed = row(17, { kind: 'tool_result', content: 'researcher crashed', metadata: { toolUseId: 'a1', isError: true, images: [] } });
    render(<SubagentCard toolUse={call} group={{ steps: [], result: failed }} runActive={false} />);
    expect(screen.getByText('failed')).toBeInTheDocument();
  });

  it('shows "stopped" when the run ended without a result (interrupt)', () => {
    render(<SubagentCard toolUse={call} group={{ steps: steps.slice(0, 1), result: null }} runActive={false} />);
    expect(screen.getByText('stopped')).toBeInTheDocument();
  });
});
