/**
 * Line diff for the Docs panel (spec §7.6/§12.2, plan C4 Task 3).
 *
 * - The common prefix and suffix are trimmed first; Myers O(ND) runs on the rest only.
 * - The search stops once D exceeds `maxD` (default 2,000): "too large to diff".
 * - No n×m table, and no full V array per step: step d stores only its d+1 reachable
 *   diagonals (k = -d, -d+2, …, d) in its own Int32Array, so the trace is bounded by
 *   (D+1)(D+2)/2 entries (the "sliced" bound) and never by D·(N+M).
 */

export type DiffOp = { t: 'eq' | 'add' | 'del'; line: string };

export interface DiffStats {
  /** Edit distance of the trimmed middle (insertions + deletions). */
  d: number;
  /** Total length of every Int32Array the search allocated. */
  allocated: number;
}

export type DiffResult = { tooLarge: false; ops: DiffOp[]; stats: DiffStats } | { tooLarge: true; stats: DiffStats };

export const MAX_D = 2000;

/** Lines without their terminators; a trailing newline does not add an empty line. */
export function splitLines(text: string): string[] {
  if (text === '') return [];
  const lines = text.split(/\r?\n/);
  if (lines[lines.length - 1] === '') lines.pop();
  return lines;
}

export function diffLines(before: string, after: string, maxD = MAX_D): DiffResult {
  const a = splitLines(before);
  const b = splitLines(after);
  let pre = 0;
  while (pre < a.length && pre < b.length && a[pre] === b[pre]) pre++;
  let suf = 0;
  while (suf < a.length - pre && suf < b.length - pre && a[a.length - 1 - suf] === b[b.length - 1 - suf]) suf++;
  const midA = a.slice(pre, a.length - suf);
  const midB = b.slice(pre, b.length - suf);

  // Lines as integer ids: the snakes compare numbers, not strings.
  const ids = new Map<string, number>();
  const toIds = (lines: string[]) => {
    const out = new Int32Array(lines.length);
    lines.forEach((l, i) => {
      let id = ids.get(l);
      if (id === undefined) {
        id = ids.size;
        ids.set(l, id);
      }
      out[i] = id;
    });
    return out;
  };
  const A = toIds(midA);
  const B = toIds(midB);
  const stats: DiffStats = { d: 0, allocated: A.length + B.length };

  const middle = myers(A, B, maxD, stats);
  if (middle === null) return { tooLarge: true, stats };

  const ops: DiffOp[] = [];
  for (let i = 0; i < pre; i++) ops.push({ t: 'eq', line: a[i]! });
  // Within a block of changes the removals come first (the unified diff convention).
  let adds: DiffOp[] = [];
  for (const [t, ia, ib] of middle) {
    if (t === 'add') {
      adds.push({ t, line: midB[ib]! });
      continue;
    }
    if (t === 'eq') {
      ops.push(...adds);
      adds = [];
    }
    ops.push({ t, line: midA[ia]! });
  }
  ops.push(...adds);
  for (let i = a.length - suf; i < a.length; i++) ops.push({ t: 'eq', line: a[i]! });
  return { tooLarge: false, ops, stats };
}

type MidOp = [DiffOp['t'], number, number];

/**
 * Myers' greedy forward search. `trace[d][(k + d) >> 1]` is the furthest x on diagonal k
 * after d edits, or -1 if that diagonal is not reachable inside the grid.
 */
function myers(A: Int32Array, B: Int32Array, maxD: number, stats: DiffStats): MidOp[] | null {
  const n = A.length;
  const m = B.length;
  const trace: Int32Array[] = [];
  let found = -1;
  for (let d = 0; d <= Math.min(maxD, n + m); d++) {
    const cur = new Int32Array(d + 1);
    stats.allocated += d + 1;
    const prev = d > 0 ? trace[d - 1]! : null;
    for (let k = -d; k <= d; k += 2) {
      let x = d === 0 ? 0 : startX(choose(prev!, d, k, n, m));
      if (x >= 0) {
        let y = x - k;
        while (x < n && y < m && A[x] === B[y]) {
          x++;
          y++;
        }
        if (x === n && y === m) found = d;
      }
      cur[(k + d) >> 1] = x;
    }
    trace.push(cur);
    if (found >= 0) break;
  }
  if (found < 0) return null;
  stats.d = found;

  // Backtrack from (n, m), repeating the forward choice per step.
  const rev: MidOp[] = [];
  let x = n;
  let y = m;
  for (let d = found; d > 0; d--) {
    const k = x - y;
    const down = choose(trace[d - 1]!, d, k, n, m) >= 0;
    const pk = down ? k + 1 : k - 1;
    const px = trace[d - 1]![(pk + d - 1) >> 1]!;
    const py = px - pk;
    // the snake after the edit
    const sx = down ? px : px + 1;
    while (x > sx) {
      x--;
      y--;
      rev.push(['eq', x, y]);
    }
    if (down) rev.push(['add', px, py]);
    else rev.push(['del', px, py]);
    x = px;
    y = py;
  }
  while (x > 0) {
    x--;
    y--;
    rev.push(['eq', x, y]);
  }
  return rev.reverse();
}

/**
 * The start on diagonal k for step d: down (insert B[y]) from k+1, or right (delete A[x]) from
 * k-1, whichever reaches further inside the grid; ties go down. Encoded without allocating:
 * x ≥ 0 = down to x, ≤ -2 = right to -(r + 2), -1 = neither fits.
 */
function choose(prev: Int32Array, d: number, k: number, n: number, m: number): number {
  const fromDown = k + 1 > d - 1 ? -1 : prev[(k + d) >> 1]!;
  const downX = fromDown >= 0 && fromDown - k <= m ? fromDown : -1;
  const fromRight = k - 1 < -(d - 1) ? -1 : prev[(k + d - 2) >> 1]!;
  const rightX = fromRight >= 0 && fromRight + 1 <= n ? fromRight + 1 : -1;
  if (downX < 0 && rightX < 0) return -1;
  return downX >= rightX ? downX : -(rightX + 2);
}

const startX = (r: number) => (r >= -1 ? r : -r - 2);

export type DiffRow = { kind: 'line'; op: DiffOp } | { kind: 'gap'; count: number; ops: DiffOp[] };

/** Unchanged runs keep `context` lines next to a change; the rest folds into a gap row. */
export function collapse(ops: DiffOp[], context = 3): DiffRow[] {
  const rows: DiffRow[] = [];
  let i = 0;
  while (i < ops.length) {
    if (ops[i]!.t !== 'eq') {
      rows.push({ kind: 'line', op: ops[i]! });
      i++;
      continue;
    }
    let j = i;
    while (j < ops.length && ops[j]!.t === 'eq') j++;
    const run = ops.slice(i, j);
    const head = i === 0 ? 0 : context;
    const tail = j === ops.length ? 0 : context;
    if (run.length > head + tail + 1) {
      for (const op of run.slice(0, head)) rows.push({ kind: 'line', op });
      const hidden = run.slice(head, run.length - tail);
      rows.push({ kind: 'gap', count: hidden.length, ops: hidden });
      for (const op of run.slice(run.length - tail)) rows.push({ kind: 'line', op });
    } else {
      for (const op of run) rows.push({ kind: 'line', op });
    }
    i = j;
  }
  return rows;
}
