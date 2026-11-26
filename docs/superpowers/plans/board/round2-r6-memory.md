# Round 2 — Phase R6: heavy-process gate, worker recycling, concurrent memory measurement

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Read first: the index (incl. "Round 2", "As built (P6a)", "As built (P3b)", "As built (P9a)"
> → Memory), spec §15.3 D5 and **§22.2 decision 3 / §22.6**. **Wave R-1: no dependency.**

**Goal:** the parity DRC (`kicad-cli pcb drc --schematic-parity`, ≈ 2.6 GB) **starts only after**
the running Freerouting JVM and the running CPU-worker job have finished (owner decision 3:
"run it only after the other heavy processes finish"); a CPU job that starts *during* the DRC is
not blocked (blocking it would starve `pcb_route` / the fill for up to 240 s), its memory is part
of the measured concurrent peak, the CPU worker does not keep ≈ 1 GB
after a big job, and the dry-run script reports the **concurrent** peak next to the sum of
per-process peaks; re-measure on ODH and report the number honestly against the ≈ 5 GB target.

**Heavy processes** (this phase's definition, spec §22.2 decision 3): the Freerouting JVM, a
CPU-worker job (`run_cpu`), and a `kicad-cli` parity DRC. Both parity paths count:
`pcb_check full` (`cc/kicad_tools/board/check.py` `cli_parity`) **and** the older generic path
`cc/kicad_tools/checks.py` (`run_kicad_check_on_dir(…, parity=True)` for `doc == "pcb"`, used by
the existing `run_drc` tool and the board render's violation markers). The netlist export
(≈ 0.6 GB, P9a) and other `kicad-cli` exports are not heavy and stay ungated.

**Architecture:** new `cc/board/heavy.py`: one `asyncio.Semaphore(1)` **per running event loop**
(a `weakref.WeakKeyDictionary` keyed by the loop, exactly like `freerouting._slots`,
`freerouting.py:339`) behind `heavy_slot(kind, *, wait_s)`. Lock order, everywhere:
**session apply lock (`ctx.session.applying()`) → heavy gate → (CPU-worker lock, momentarily) →
`BOARD_CLI_LOCK`**; nothing acquires an earlier lock while holding a later one. In `cli_parity`
the gate is entered **only after** `_copy_project` / `save_copy` (which takes the apply lock
without a timeout) has finished — around the `BOARD_CLI_LOCK` + `kicad-cli` part only; test
`test_parity_gate_not_held_during_copy` (the fill's DSN export releases `BOARD_CLI_LOCK` before Freerouting
starts; `run_cpu` takes only the worker lock). `run_freerouting` holds the gate for the JVM run.
A parity DRC holds the gate, **waits until the CPU worker is idle** (acquires
`worker._get_lock()` and releases it at once — it never holds the worker lock during the DRC, so
route solves, fill post-processing and metrics jobs are never starved by a 120 s parity run;
a job that starts during the DRC is bounded by its own `run_cpu` timeout — ≤ 60 s
(`PLACE_TIMEOUT_S`), most ≤ 30 s — and the worker is kept small by recycling; R6 Task 3 measures
whether such overlap moves the concurrent peak and reports it), then takes `BOARD_CLI_LOCK`; every wait is bounded. `run_cpu` recycles
the worker **while it still holds the worker lock**, so a queued job never starts on a worker
that is about to be killed.

`cc/` = `backend/src/copperchat/`.

## Global Constraints

- Index Global Constraints (every long call has an explicit timeout; cancellation shielded
  where the index says so). `MAX_CONCURRENT_RUNS = 2` in `freerouting.py` stays but is
  effectively 1 through the gate — record it.
- Waits are bounded: the gate and the worker lock together by `HEAVY_WAIT_S` (120 s), the
  following `BOARD_CLI_LOCK` wait by the remaining part of it. The fill's gate wait is bounded by
  `min(HEAVY_WAIT_S, timeout_s)` and subtracted from Freerouting's own timeout, so
  `FILL_WORST_CASE_S` (1005 s) does not grow. `pcb_check full` gains at most `HEAVY_WAIT_S`:
  add a row `pcb_check (full)` to the index's worst-case table (today's parity
  API DRC `drc_call_timeout(60)` = 105 + `HEAVY_WAIT_S` 120 + parity `BOARD_PARITY_TIMEOUT_S` 120 +
  metrics `run_cpu(timeout_s=30, wait_s=HEAVY_WAIT_S)` 150 — `check.py:440` passes no `wait_s`
  today (unbounded); bound it by `HEAVY_WAIT_S` — parity never holds the worker lock, so this
  only bounds waiting behind other CPU jobs — → 495 s; test `test_quick_check_runs_during_parity`) and record it. The gate is process-wide (all projects); cross-project serialisation of parity
  DRCs is intended (memory is a host budget) — record it in "As built (R6)".

## Review Focus (this phase)

1. Gate held across a cancelled turn → released (`async with` + `CancelledError`).
   → `test_gate_released_on_cancel`.
2. Parity waiting forever behind a 600 s fill → `board_compute_timeout` after `HEAVY_WAIT_S`.
   → `test_parity_waits_then_times_out`.
3. A queued second `run_cpu` job must not run on a worker that is recycled under it.
   → `test_recycle_before_next_queued_job`.
4. The legacy parity path forgotten. → `test_run_kicad_check_parity_gated`.
5. Per-test event loops (`asyncio_default_fixture_loop_scope = function`) → a fresh semaphore per
   loop, no "bound to a different event loop" error. → `test_slot_per_loop`.

---

### Task 1: `heavy_slot` and its users

**Files:** Create `cc/board/heavy.py`; Modify `cc/board/freerouting.py` (`run_freerouting`
body wrapped), `cc/kicad_tools/board/check.py` (`cli_parity`), `cc/kicad_tools/checks.py`
(`run_kicad_check_on_dir` when `doc == "pcb" and parity`); Test:
`backend/tests/unit/board/test_heavy.py` (new), `backend/tests/unit/board/test_freerouting_unit.py`,
new `backend/tests/unit/test_checks_parity_gate.py` (`cc/kicad_tools/checks.py` has only a
KiCad-level test today, `tests/kicad/edit/test_checks_real.py`).

**Interfaces:** `heavy_slot(kind: Literal["freerouting", "parity"], *, wait_s: float) ->
AsyncContextManager[None]` — for `"parity"` it also waits (same `wait_s` budget) until the
CPU worker is idle, without holding its lock afterwards; raises `StructuredToolError(code="board_compute_timeout",
details={"waiting_for": <"freerouting" | "parity" | "cpu_job">})` after `wait_s`.
`HEAVY_WAIT_S = 120.0`. Failure modes per caller: `run_freerouting` maps a gate timeout to a
`FreeroutingRun(timed_out=True)` exactly like its existing slot wait (`freerouting.py:369-375`),
so the fill reports `fill_not_acceptable` / `timeout` as today; `pcb_check full` raises
`board_compute_timeout`; the legacy path's callers: the existing DRC check tool returns
`board_compute_timeout`, the board view's violation markers (`views/render.py:92`
`cached_violations`) degrade to **no markers** (logged), never fail the render. Each adds at most
`HEAVY_WAIT_S` to its call — record it.

- [ ] **Step 1: Failing tests:** `test_freerouting_gate_timeout_is_timed_out_run`,
  `test_render_markers_degrade_on_gate_timeout`, `test_slot_serialises` (two tasks, second enters after the
  first exits), `test_slot_per_loop` (two `asyncio.run` calls in a row each get a working slot),
  `test_gate_released_on_cancel`, `test_parity_waits_then_times_out` (fake held slot,
  `wait_s=0.05` → `board_compute_timeout` with `waiting_for == "freerouting"`),
  `test_parity_waits_for_cpu_job` (a running `run_cpu` job holds the worker lock → parity enters
  only after it; with `wait_s` shorter → `waiting_for == "cpu_job"`),
  `test_cpu_job_runs_during_parity` (a `run_cpu` call started while the parity slot is held
  completes without waiting for it),
  `test_freerouting_holds_slot` (fake runtime: slot busy during the subprocess, free after),
  `test_run_kicad_check_parity_gated` (fake `run_kicad_cli`: called inside the slot for a pcb
  parity check, outside it for `sch` ERC and pcb without parity).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): heavy-process gate for Freerouting, CPU jobs and parity DRC`.

### Task 2: CPU-worker recycling

**Files:** Modify `cc/board/worker.py`; Test: `backend/tests/unit/board/test_worker.py` (append).

**Interfaces:** `WORKER_RECYCLE_MB = 512`; in `run_cpu`, after `af.result()` succeeded and
**before** the `finally: lock.release()` (`worker.py:126-133`), read the worker's `VmRSS` from
`/proc/<pid>/status` (Linux; skip silently elsewhere) and call `_kill_and_replace()` when above.
One log line per recycle.

- [ ] **Step 1: Failing tests:** `test_recycle_above_threshold` (monkeypatched RSS reader →
  next job runs in a new PID), `test_no_recycle_below`, `test_recycle_before_next_queued_job`
  (two jobs queued; the first triggers a recycle; the second completes normally in the new PID).
- [ ] **Step 2–4:** run FAIL → implement → PASS. **Step 5: Commit** `feat(board): recycle the CPU worker after large jobs`.

### Task 3: Concurrent peak in the dry-run script, re-measure

**Files:** Modify `scripts/odh-dry-run.py` (`PeakSampler` / `Memory` / `build_report`).

**Interfaces:** the sampler (every `SAMPLE_S` 0.5 s) also records, per sample, Σ `VmRSS` over
the api-server, JVM, CPU worker, raster worker and every `kicad-cli` descendant. The report
gains "Concurrent peak" = max over samples (a **lower bound**: 0.5 s samples can miss short
spikes; labelled so), with the step it happened in and each part's share, next to the existing
sum of per-process `VmHWM` peaks (the **upper bound**). The D5 comparison states both.

- [ ] **Step 1: Failing tests** in the existing `backend/tests/unit/test_odh_dry_run.py` (it
  already loads the script by path): `test_concurrent_peak` for a new pure helper
  `concurrent_peak(samples) -> tuple[float, str, dict[str, float]]` (total MB, step, shares) on
  hand-made samples, and `test_report_has_memory_section` extended by the "Concurrent peak
  (lower bound)" and "Sum of peaks (upper bound)" lines. Run — FAIL; implement; run — PASS.
- [ ] **Step 2: Measure** on an ODH copy at 80 × 100 mm with P9a's answers (kicad-base +
  fetched Freerouting, `timeout 3600`): record both bounds, the step of the concurrent peak, and
  the parity DRC's share in "As built (R6)". Expected from P9a's numbers: api-server ≈ 3.1 GB +
  parity ≈ 2.6 GB ≈ 5.7 GB — report the real values. **If the concurrent peak is > 5.0 GB:** state
  it in the done report as an owner decision (round-2 plan call 9: accept the number, or a later
  own parity check on the netlist); do not implement the replacement here.
- [ ] **Step 3: Commit** `feat(scripts): concurrent memory peak in odh-dry-run`.

### Task 4: Index, CI, review

- [ ] "As built (R6)" (both bounds, gate waits observed, the `pcb_check (full)` worst-case row),
  CHANGELOG line; lint/type; PR `board round 2 R6: heavy-process gate and memory`; CI green;
  review-loop; done report with the concurrent peak.
