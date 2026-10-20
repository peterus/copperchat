import { describe, expect, it } from 'vitest';
import { collapse, type DiffOp, diffLines, splitLines } from './lineDiff';

const sides = (ops: DiffOp[]) => ({
  a: ops.filter((o) => o.t !== 'add').map((o) => o.line),
  b: ops.filter((o) => o.t !== 'del').map((o) => o.line),
});
const edits = (ops: DiffOp[]) => ops.filter((o) => o.t !== 'eq').length;

/** Reference LCS edit distance (small inputs only). */
function lcsDistance(a: string[], b: string[]): number {
  const dp = Array.from({ length: a.length + 1 }, () => new Array<number>(b.length + 1).fill(0));
  for (let i = 1; i <= a.length; i++)
    for (let j = 1; j <= b.length; j++)
      dp[i]![j] = a[i - 1] === b[j - 1] ? dp[i - 1]![j - 1]! + 1 : Math.max(dp[i - 1]![j]!, dp[i]![j - 1]!);
  return a.length + b.length - 2 * dp[a.length]![b.length]!;
}

function rng(seed: number) {
  let s = seed;
  return () => {
    s = (s * 1103515245 + 12345) & 0x7fffffff;
    return s / 0x7fffffff;
  };
}

describe('diffLines', () => {
  it('diffs a simple change with additions and removals', () => {
    const r = diffLines('a\nb\nc\n', 'a\nx\nc\nd\n');
    expect(r.tooLarge).toBe(false);
    if (r.tooLarge) return;
    expect(r.ops).toEqual([
      { t: 'eq', line: 'a' },
      { t: 'del', line: 'b' },
      { t: 'add', line: 'x' },
      { t: 'eq', line: 'c' },
      { t: 'add', line: 'd' },
    ]);
  });

  it('handles empty sides and identical texts', () => {
    const add = diffLines('', 'a\nb');
    expect(!add.tooLarge && add.ops.map((o) => o.t)).toEqual(['add', 'add']);
    const del = diffLines('a\nb\n', '');
    expect(!del.tooLarge && del.ops.map((o) => o.t)).toEqual(['del', 'del']);
    const same = diffLines('a\nb\n', 'a\nb\n');
    expect(!same.tooLarge && same.ops.map((o) => o.t)).toEqual(['eq', 'eq']);
    expect(splitLines('a\r\nb\r\n')).toEqual(['a', 'b']);
  });

  it('is a minimal edit script that reproduces both sides (random cases)', () => {
    const rand = rng(42);
    for (let round = 0; round < 300; round++) {
      const alphabet = 'abcde';
      const gen = () =>
        Array.from({ length: Math.floor(rand() * 14) }, () => alphabet[Math.floor(rand() * alphabet.length)]!);
      const a = gen();
      const b = gen();
      const r = diffLines(a.join('\n'), b.join('\n'));
      if (r.tooLarge) throw new Error('unexpected');
      expect(sides(r.ops)).toEqual({ a, b });
      expect(edits(r.ops)).toBe(lcsDistance(a, b));
    }
  });

  it('stops above the D cap', () => {
    const a = Array.from({ length: 30 }, (_, i) => `a${i}`).join('\n');
    const b = Array.from({ length: 30 }, (_, i) => `b${i}`).join('\n');
    expect(diffLines(a, b, 59).tooLarge).toBe(true);
    expect(diffLines(a, b, 60).tooLarge).toBe(false);
  });

  it('perf: two 4,000-line docs with 50 changes diff in under 200 ms', () => {
    const a = Array.from({ length: 4000 }, (_, i) => `line ${i} of the requirements`);
    const b = [...a];
    for (let c = 0; c < 50; c++) b[40 + c * 79] = `changed ${c}`;
    const t0 = performance.now();
    const r = diffLines(a.join('\n'), b.join('\n'));
    const ms = performance.now() - t0;
    expect(r.tooLarge).toBe(false);
    if (!r.tooLarge) expect(edits(r.ops)).toBe(100);
    expect(ms).toBeLessThan(200);
  });

  it('perf: near the cap (D ≈ 1,900, N+M ≈ 8,000) the trace stays within the sliced bound', () => {
    const a = Array.from({ length: 4000 }, (_, i) => `same ${i}`);
    const b = [...a];
    // 950 replaced lines spread over the doc: 950 deletions + 950 insertions.
    for (let c = 0; c < 950; c++) b[2 + c * 4] = `new ${c}`;
    const t0 = performance.now();
    const r = diffLines(a.join('\n'), b.join('\n'));
    const ms = performance.now() - t0;
    expect(r.tooLarge).toBe(false);
    const { d, allocated } = r.stats;
    expect(d).toBe(1900);
    const nm = 8000;
    // Sliced: (D+1)(D+2)/2 trace entries plus the two id arrays; a V array per step would be D·(N+M).
    expect(allocated).toBeLessThanOrEqual(((d + 1) * (d + 2)) / 2 + nm);
    expect(allocated).toBeLessThan(d * nm);
    expect(ms).toBeLessThan(200);
  });
});

describe('collapse', () => {
  const eq = (n: number, p = 'e'): DiffOp[] => Array.from({ length: n }, (_, i) => ({ t: 'eq', line: `${p}${i}` }));
  it('keeps three context lines around changes and folds the rest', () => {
    const ops: DiffOp[] = [...eq(10, 'a'), { t: 'add', line: 'x' }, ...eq(10, 'b'), { t: 'del', line: 'y' }, ...eq(2, 'c')];
    const rows = collapse(ops);
    const kinds = rows.map((r) => (r.kind === 'gap' ? `gap${r.count}` : r.op.t));
    expect(kinds).toEqual([
      'gap7', 'eq', 'eq', 'eq', 'add', 'eq', 'eq', 'eq', 'gap4', 'eq', 'eq', 'eq', 'del', 'eq', 'eq',
    ]);
  });
  it('does not fold a run that would hide a single line', () => {
    const ops: DiffOp[] = [{ t: 'add', line: 'x' }, ...eq(7), { t: 'add', line: 'y' }];
    expect(collapse(ops).every((r) => r.kind === 'line')).toBe(true);
  });
});
