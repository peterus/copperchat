# Round 2 — Phase R6: heavy-process gate, worker recycling, concurrent memory measurement

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Read first: the index (incl. "Round 2", "As built (P6a)", "As built (P3b)", "As built (P9a)"
> → Memory), spec §15.3 D5 and **§22.2 decision 3 / §22.6**. **Wave R-1: no dependency.**

**Goal:** the parity DRC (`kicad-cli`, ≈ 2.6 GB) never runs while Freerouting or another heavy
board process runs (owner decision 3), the CPU worker does not keep ≈ 1 GB after a big job,
and the dry-run script reports the **concurrent** peak instead of the sum of per-process peaks;
re-measure on ODH and report the number honestly against the ≈ 5 GB target.

**Architecture:** new `cc/board/heavy.py` with one process-wide `asyncio.Semaphore(1)` wrapped
in `heavy_slot(kind, *, wait_s)`; `run_freerouting` holds it for the whole JVM run, `cli_parity`
for the `kicad-cli pcb drc` call (acquired **before** `BOARD_CLI_LOCK`; lock order is always
heavy → CLI, and no code acquires the heavy gate while holding `BOARD_CLI_LOCK` — the fill's
DSN export releases the CLI lock before Freerouting starts). `run_cpu` checks the worker's RSS after each job and
recycles it above `WORKER_RECYCLE_MB` (reusing `worker._kill_and_replace`).

`cc/` = `backend/src/copperchat/`.

## Global Constraints

- Index Global Constraints (every long call has an explicit timeout; cancellation shielded
  where the index says so). `MAX_CONCURRENT_RUNS = 2` in `freerouting.py` becomes effectively 1
  through the gate — record it.
- A gate wait counts against the tool's own deadline (fill: before Freerouting starts, inside
  its `timeout_s`; check: `HEAVY_WAIT_S` 120 s) — the index worst cases must not grow; if one
  would, cap the wait instead and record it.

## Review Focus (this phase)

1. Gate held across a cancelled turn → released (`async with` + `CancelledError`).
   → `test_gate_released_on_cancel`.
2. Parity waiting forever behind a 600 s fill → `board_compute_timeout` after `HEAVY_WAIT_S`.
   → `test_parity_waits_then_times_out`.
3. Recycling during a running job must not kill it (check only after the job's future is done).
   → `test_recycle_only_between_jobs`.

---

### Task 1: `heavy_slot` and its two users

**Files:** Create `cc/board/heavy.py`; Modify `cc/board/freerouting.py` (`run_freerouting`
body wrapped), `cc/kicad_tools/board/check.py` (`cli_parity`); Test:
`backend/tests/unit/board/test_heavy.py` (new), `backend/tests/unit/board/test_freerouting_unit.py`.

**Interfaces:** `heavy_slot(kind: Literal["freerouting", "parity"], *, wait_s: float) ->
AsyncContextManager[None]`; raises `StructuredToolError(code="board_compute_timeout",
details={"waiting_for": <kind holding it>})` after `wait_s`. `HEAVY_WAIT_S = 120.0`. Fill: wait
bounded by `min(HEAVY_WAIT_S, timeout_s)` and subtracted from Freerouting's timeout.

- [ ] **Step 1: Failing tests:** `test_slot_serialises` (two tasks, second enters after the
  first exits), `test_gate_released_on_cancel`, `test_parity_waits_then_times_out` (fake held
  slot, `wait_s=0.05` → `board_compute_timeout` with `waiting_for == "freerouting"`),
  `test_freerouting_holds_slot` (fake runtime: slot busy during the subprocess, free after).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): heavy-process gate for Freerouting and parity DRC`.

### Task 2: CPU-worker recycling

**Files:** Modify `cc/board/worker.py`; Test: `backend/tests/unit/board/test_worker.py` (append).

**Interfaces:** `WORKER_RECYCLE_MB = 512`; after a job's result is delivered, `run_cpu` reads
the worker's `VmRSS` from `/proc/<pid>/status` (Linux; skip silently elsewhere) and calls
`_kill_and_replace()` when above. One log line per recycle.

- [ ] **Step 1: Failing tests:** `test_recycle_above_threshold` (monkeypatched RSS reader →
  next job runs in a new PID), `test_no_recycle_below`, `test_recycle_only_between_jobs`.
- [ ] **Step 2–4:** run FAIL → implement → PASS. **Step 5: Commit** `feat(board): recycle the CPU worker after large jobs`.

### Task 3: Concurrent peak in the dry-run script, re-measure

**Files:** Modify `scripts/odh-dry-run.py` (`PeakSampler` / `Memory` / `build_report`).

**Interfaces:** the sampler (every `SAMPLE_S` 0.5 s) also records, per sample, Σ `VmRSS` over
the api-server, JVM, CPU worker, raster worker and every `kicad-cli` descendant; the report
gains "Concurrent peak" (max over samples, with the step it happened in and each part's share)
next to the existing per-process peaks; the D5 comparison uses the concurrent peak.

- [ ] **Step 1: Failing tests** in the existing `backend/tests/unit/test_odh_dry_run.py` (it
  already loads the script by path): `test_concurrent_peak` for a new pure helper
  `concurrent_peak(samples) -> tuple[float, str, dict[str, float]]` (total MB, step, shares) on
  hand-made samples, and `test_report_has_memory_section` extended by the "Concurrent peak" line.
  Run — FAIL; implement; run — PASS.
- [ ] **Step 2: Measure** on an ODH copy at 80 × 100 mm with P9a's answers (kicad-base +
  fetched Freerouting, `timeout 3600`): record concurrent peak, its step, and the parity DRC's
  share in "As built (R6)". Expected from P9a's numbers: api-server ≈ 3.1 GB + parity ≈ 2.6 GB
  ≈ 5.7 GB — report the real value. **If > 5.0 GB:** state it in the done report as an owner
  decision (round-2 plan call 9: accept the number, or a later own parity check on the netlist);
  do not implement the replacement here.
- [ ] **Step 3: Commit** `feat(scripts): concurrent memory peak in odh-dry-run`.

### Task 4: Index, CI, review

- [ ] "As built (R6)" (numbers, gate waits observed), CHANGELOG line; lint/type; PR `board
  round 2 R6: heavy-process gate and memory`; CI green; review-loop; done report with the
  concurrent peak.
