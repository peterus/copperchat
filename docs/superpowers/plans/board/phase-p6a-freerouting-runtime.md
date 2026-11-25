# Phase P6a — Freerouting runtime, sandboxed runner, pure post-processor

> Index contracts: B-C1 (`freerouting`, `freerouting_home`), B-C12 (`PostResult`,
> `postprocess`), B-C13 (`FreeroutingRuntime`, `find_runtime`, `FreeroutingRun`,
> `run_freerouting`); consumes B-C2, B-C5 (`analyse_bends`, `CHAMFER_MIN`, `JOG_MIN`), B-C7
> (`connections`, `routed_length`). Index call 11, Global Constraints "Freerouting". Spec §4
> C7/C10/C15, §10.3 (runtime, post-processor), §16 G6, §18.1 (post-processor), §18.2
> (Freerouting in CI). Spike 4A Q4 (`spikes/4a-board/{freerouting,fetch_deps}.sh|py`, port,
> never import). **Wave 4: starts when P3a is merged** (pure/infra; parallel to P3b, P5a, P2b).

**Goal:** a checksum-pinned Freerouting 2.5.0 JAR + Temurin 25 JRE in the app image and in the
CI kicad job (one fetch script), a runner that starts the JVM headless under resource limits
without network access and kills it on timeout or turn stop, and a pure post-processor that
merges, chamfers 90° corners to two 45° bends and lists every connection that must be ripped.
No user-visible tool (P6b adds `pcb_route_fill`).

`cc/` = `backend/src/copperchat/`.

**Files:**
- Create: `docker/freerouting.lock`, `scripts/fetch-freerouting.sh`,
  `LICENSES/freerouting.md` (GPL-3.0 notice: version, source URL, "unmodified, separate
  process", corresponding source = tag `v2.5.0`), `LICENSES/temurin.md` (GPL-2.0 with
  Classpath Exception notice; the JRE's own `legal/` tree and `NOTICE` ship inside
  `/opt/freerouting/jre`), `cc/board/freerouting.py`, `cc/board/router/chamfer.py`
  (`cc/board/router/__init__.py` is P5a's; if P5a is not merged yet, create it empty and
  rebase — append-only)
- Modify: `docker/app/Dockerfile` (fetch stage + copy + notices), `scripts/app-image-tools.sh`
  (Java/JAR check — not in the index's P6a ownership row, reported), `.github/workflows/ci.yml`
  (kicad job: cache + fetch step + env), `cc/config.py` (append `freerouting`,
  `freerouting_home`)
- Tests: `backend/tests/unit/board/{test_fetch_freerouting,test_freerouting_unit,test_chamfer}.py`,
  `backend/tests/unit/test_config.py` (append), `backend/tests/kicad/board/test_freerouting_runtime.py`,
  `backend/tests/kicad/board/conftest.py` (append the `freerouting_rt` fixture)

**Runtime tests live under `tests/kicad/board/` (`-m kicad`)** although they need no KiCad:
only the kicad job fetches the JRE/JAR. The `freerouting_rt` fixture returns
`find_runtime(Settings())`; `None` → `pytest.fail("Freerouting runtime missing in CI: run
scripts/fetch-freerouting.sh")` when `os.environ.get("CI") == "true"`, else
`pytest.skip("Freerouting runtime missing locally (COPPERCHAT_FREEROUTING_HOME=…)")` with the
path in the reason. P6b and P9a reuse it.

---

### Task 1: Lock file and checksum-verified fetch script

**Interfaces:** `docker/freerouting.lock` (shell `KEY=value`, index call 11):
`FREEROUTING_VERSION=2.5.0`,
`FREEROUTING_URL=https://github.com/freerouting/freerouting/releases/download/v2.5.0/freerouting-2.5.0.jar`,
`FREEROUTING_SHA256=<64 hex>`, `TEMURIN_URL=<Eclipse Temurin 25 JRE, linux x64 hotspot tarball
from github.com/adoptium/temurin25-binaries/releases>`, `TEMURIN_SHA256=<64 hex>`. The executor
picks the newest Temurin 25.0.x GA JRE at execution time, takes the SHA-256 from the release's
`.sha256.txt` **and** recomputes it from a download (both must agree), computes the JAR's
SHA-256 from a download (Freerouting publishes no checksum file — record that), and records
URLs, hashes and sizes in "As built (P6a)".

`scripts/fetch-freerouting.sh <dest>` (bash, `set -euo pipefail`): sources the lock file
(path relative to the script, overridable by `FREEROUTING_LOCK` for tests); downloads with
`python3` `urllib.request` (the kicad-base image has `python3` but no `curl`; `file://` URLs
work too, which the tests use) into `<dest>/.dl.tmp/`; verifies both files with `sha256sum
-c`; **any mismatch → message naming the file, expected and actual hash, exit 1, `<dest>`
untouched**; then extracts the JRE to `<dest>/jre` (`--strip-components=1`), installs the
JAR as `<dest>/freerouting-${FREEROUTING_VERSION}.jar`, copies the lock to `<dest>/.lock`,
removes `.dl.tmp`. Idempotent: if `<dest>/.lock` equals the lock file, the JAR hash matches
and `<dest>/jre/bin/java -version` exits 0, it prints `up to date` and exits 0 (CI cache hit).
Total download timeout 300 s.

- [ ] **Step 1: Failing unit tests** (`test_fetch_freerouting.py`, run the script with
  `subprocess` and `timeout=60`, a temp lock pointing at `file://` fixtures built in
  `tmp_path`: a fake JAR (bytes) and a tarball with `jdk-25/bin/java` = a shell script that
  prints `openjdk version "25.0.1"`): `test_fetch_ok` (dest has `jre/bin/java`,
  `freerouting-2.5.0.jar`, `.lock`; no `.dl.tmp`); `test_jar_hash_mismatch_refused` (exit ≠ 0,
  stderr names `freerouting` and both hashes, dest empty); `test_jre_hash_mismatch_refused`;
  `test_second_run_up_to_date` (stdout `up to date`, mtime of the JAR unchanged);
  `test_lock_change_refetches`; `test_lock_format` (the real `docker/freerouting.lock` has
  exactly the five keys, both hashes `^[0-9a-f]{64}$`, both URLs `https://`, version `2.5.0`).
- [ ] **Step 2: Run** `uv run pytest tests/unit/board/test_fetch_freerouting.py -q` — FAIL.
  **Step 3: Implement** (determine the real URLs and hashes, fill the lock file).
  **Step 4: Run** — PASS; also `timeout 600 bash scripts/fetch-freerouting.sh /tmp/fr` against
  the real URLs, then `/tmp/fr/jre/bin/java -version` prints `25.`.
- [ ] **Step 5: Commit** `build(board): pinned Freerouting 2.5.0 + Temurin 25 fetch script`.

### Task 2: App image, CI kicad job, settings

**Interfaces:** B-C1 `Settings.freerouting: bool = Field(default=True,
validation_alias="COPPERCHAT_FREEROUTING")`, `Settings.freerouting_home: Path =
Field(default=Path("/opt/freerouting"), validation_alias="COPPERCHAT_FREEROUTING_HOME")`.

Dockerfile: a stage `FROM ${KICAD_BASE} AS freerouting` (reuses the already pulled base; it
has `python3`, `tar`, `sha256sum`), `USER root`, `COPY docker/freerouting.lock
scripts/fetch-freerouting.sh /tmp/fr/`, `RUN bash /tmp/fr/fetch-freerouting.sh /opt/freerouting
&& rm -rf /opt/freerouting/.lock`; final stage `COPY --from=freerouting /opt/freerouting
/opt/freerouting` (root-owned, world-readable) and `COPY LICENSES/
/usr/share/doc/copperchat/LICENSES/`. No `ENV` needed (the setting defaults to
`/opt/freerouting`). Image size delta measured with `docker image inspect --format
'{{.Size}}'` before/after on the same base (spec G6 expects ≈ +190 MB) → "As built".

`scripts/app-image-tools.sh` (inside the existing `docker run … --user 1000:1000` block):
`/opt/freerouting/jre/bin/java -version 2>&1 | grep -q 'version "25'`, `test -r
/opt/freerouting/freerouting-2.5.0.jar`, `test -r
/usr/share/doc/copperchat/LICENSES/freerouting.md`, and `python -c "from copperchat.board.freerouting
import find_runtime; from copperchat.config import Settings; assert find_runtime(Settings())"`
(needs Task 3; this line is added in Task 3's commit). The `e2e` job already runs the script.

`ci.yml` kicad job, before `uv run pytest`: job `env` gains
`COPPERCHAT_FREEROUTING_HOME: ${{ github.workspace }}/.cache/freerouting`; steps
`- uses: actions/cache@v4` (`path: .cache/freerouting`, `key:
freerouting-${{ hashFiles('docker/freerouting.lock') }}`) and `- run: bash
scripts/fetch-freerouting.sh "$COPPERCHAT_FREEROUTING_HOME"` with `working-directory: .`
(a cache hit still runs the script, which verifies and prints `up to date`).
`.gitignore` gains `/.cache/` (not ignored today).

- [ ] **Step 1: Failing unit tests** (`test_config.py`): `test_freerouting_defaults`
  (`freerouting is True`, `freerouting_home == Path("/opt/freerouting")`),
  `test_freerouting_env` (`COPPERCHAT_FREEROUTING=off` → False,
  `COPPERCHAT_FREEROUTING_HOME=/x` → `Path("/x")`). `test_ci_kicad_job_fetches_freerouting`
  (regex over `ci.yml` as `tests/unit/test_sharding.py` does — PyYAML is not a dependency):
  the kicad job has a cache step keyed on `hashFiles('docker/freerouting.lock')`,
  a step running `scripts/fetch-freerouting.sh`, and the env variable; the step order is cache
  → fetch → pytest.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** Settings, Dockerfile, ci.yml, notices.
  **Step 4: Run** — PASS; local `docker build -f docker/app/Dockerfile …` with `timeout 3600`,
  `bash scripts/app-image-tools.sh copperchat:dev` → OK; record the size delta.
- [ ] **Step 5: Commit** `build(board): Freerouting runtime in the app image and the CI kicad job`.

### Task 3: Sandboxed runner `cc/board/freerouting.py`

**Interfaces:** B-C13 exactly, plus `extra_args: Sequence[str] = ()` on `run_freerouting`
(P6b's plane-layer fallback may need Freerouting options; B-C13 deviation recorded).
Module constants: `JAVA_OPTS = ("-Xmx1g", "-XX:MaxMetaspaceSize=256m",
"-Djava.awt.headless=true")`, `TIMEOUT_DEFAULT_S = 300`, `TIMEOUT_MAX_S = 600`,
`CPU_MARGIN_S = 30`, `NOFILE = 256`, `NO_NETWORK_OPTIONS: tuple[str, ...]` (the 2.5.0
options that switch off analytics/telemetry, update checks and the built-in API server —
the executor reads them from `java -jar freerouting-2.5.0.jar --help` and from the settings
file Freerouting writes on first start, and records the exact names and that file's path in
"As built"; candidates to confirm: `--usage_and_diagnostic_data.disable_analytics=true`,
`--api_server.enabled=false`), `PROXY_BLACKHOLE = ("-Dhttp.proxyHost=127.0.0.1",
"-Dhttp.proxyPort=9", "-Dhttps.proxyHost=127.0.0.1", "-Dhttps.proxyPort=9",
"-DsocksProxyHost=127.0.0.1", "-DsocksProxyPort=9", "-Djava.net.useSystemProxies=false")`.

- `find_runtime(settings)`: `None` when `settings.freerouting` is off, when `<home>/jre/bin/java`
  is not an executable regular file or `<home>/freerouting-2.5.0.jar` is missing, or when
  `java -version` (timeout 15 s) does not report `"25.`; result cached per
  `(home, mtime of java, mtime of jar)`; `version = "2.5.0"`.
- `build_command(rt, dsn, ses, *, passes, workdir, extra_args, unshare: bool) -> list[str]`
  (pure): `[unshare -r -n --]? java *JAVA_OPTS *PROXY_BLACKHOLE -Duser.home=<workdir> -jar
  <jar> -de <dsn> -do <ses> --router.autorouter.max_passes=<passes> --gui.enabled=false
  *NO_NETWORK_OPTIONS *extra_args` (the passes option is the spike's; confirm with `--help`).
- `network_namespace_available() -> bool`: runs `unshare -r -n -- true` once (timeout 5 s),
  cached. Verified while planning: in `ghcr.io/peterus/copperchat-kicad-base:52b43fb8…` under
  `docker run` defaults `unshare -r -n true` fails with "Operation not permitted" (`unshare`
  itself is present, util-linux) — so in CI and in a default PROD container the options + proxy
  blackhole are the protection and the no-connection test below is the proof (spec §10.3).
- `run_freerouting`: `timeout_s` clamped to `(0, TIMEOUT_MAX_S]`, `passes` to `1..100`;
  `asyncio.create_subprocess_exec(..., cwd=workdir, env={"HOME": str(workdir), "PATH":
  "/usr/bin:/bin", "LANG": "C"}, stdout=<workdir>/freerouting.log, stderr=STDOUT,
  start_new_session=True, preexec_fn=_limits)` where `_limits` sets `RLIMIT_CPU` = (timeout_s +
  30, timeout_s + 35) and `RLIMIT_NOFILE` = (256, 256) — **never `RLIMIT_AS`**; wait with
  `asyncio.wait_for(proc.wait(), timeout_s)`; on timeout `os.killpg(pgid, SIGKILL)`, reap,
  `timed_out=True`; on `CancelledError` kill the group, reap under `_uncancellable`
  (`cc/kicad_session/session.py:145`), re-raise. `log_tail` = last 4096 bytes of the log.
  Returns even on non-zero exit (P6b decides).

- [ ] **Step 1: Failing unit tests** (`test_freerouting_unit.py`, no JVM): `test_command_shape`
  (order and every constant; `--gui.enabled=false`; no `-Xms`); `test_command_with_unshare`;
  `test_no_rlimit_as` (call `_limits` in a forked child via `multiprocessing` spawn helper that
  returns `resource.getrlimit` values: CPU soft = timeout + 30, NOFILE 256, `RLIMIT_AS`
  unchanged from the parent); `test_timeout_clamped` (`timeout_s=900` → 600);
  `test_find_runtime_missing` (empty `tmp_path` home → `None`); `test_find_runtime_flag_off`;
  `test_find_runtime_wrong_java` (fake `java` script printing `"21.0.2"` → `None`);
  `test_find_runtime_ok_fake` (fake script printing `"25.0.1"`, empty JAR file).
- [ ] **Step 2: Failing runtime tests** (`tests/kicad/board/test_freerouting_runtime.py`,
  `freerouting_rt` fixture; DSN inputs are exported in the test from the `ecc83` fixture board
  through `api.export_specctra_dsn` on an `ecc83_session` — module-scoped, never written by
  hand): `test_jvm_starts_under_limits` (routes ecc83's DSN with `passes=5`, `returncode ==
  0`, `.ses` exists and starts with `(session`, `seconds < 60`); `test_timeout_kills`
  (`timeout_s=1`, ecc83 DSN with `passes=100` — JVM start-up alone takes > 1 s, measured in
  Step 5 and recorded; `timed_out` True, the process group is gone: `/proc/<pid>` absent); `test_cancel_kills_no_zombie` (start, cancel
  the task after 1 s → `CancelledError`, `/proc/<pid>` absent — a zombie would still have a
  `/proc` entry — and `os.waitpid(pid, WNOHANG)` raises `ChildProcessError`);
  `test_no_network_attempt` — **method**: the test opens its own listening TCP socket on
  127.0.0.1, builds the command with `PROXY_BLACKHOLE` pointing at that port (helper
  parameter), and while the JVM runs samples `/proc/<pid>/task/*/fd` every 50 ms, resolving
  `socket:[inode]` links against `/proc/<pid>/net/{tcp,tcp6,udp,udp6}`; asserts the listener
  accepted 0 connections and no TCP/UDP inet socket was ever seen (Unix sockets are allowed).
  Justification: no root or `strace` is needed, it runs in the CI container, the listener
  catches every proxied HTTP(S)/SOCKS attempt (Java's URL stack) and the fd scan catches
  direct sockets that bypass the proxy settings; sampling can miss a socket that lives < 50 ms,
  which the listener covers for proxied traffic. When `network_namespace_available()` is True
  the test additionally asserts the command starts with `unshare`. Record which mode CI used.
  `test_settings_file_has_analytics_off` (after a run, the settings file Freerouting wrote under
  `workdir` shows the analytics/update keys disabled — path recorded in Task 3).
- [ ] **Step 3: Run** `uv run pytest tests/unit/board/test_freerouting_unit.py -q` and, with the
  runtime fetched, `uv run pytest -m kicad tests/kicad/board/test_freerouting_runtime.py` — FAIL.
  **Step 4: Implement**; add the `find_runtime` line to `scripts/app-image-tools.sh`.
  **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(board): sandboxed Freerouting runner (limits, no network, kill on cancel)`.

### Task 4: Post-processor `cc/board/router/chamfer.py` (tests first)

**Interfaces:** B-C12 `postprocess(model, kiids) -> PostResult(replace, rip, merged,
chamfered)`, pure, module-level (runs in `run_cpu`), no kipy. Extension (B-C12 deviation,
recorded): `PostResult.reasons: tuple[tuple[str, str], ...] = ()` — (rip key, reason ∈
`acute`, `non_octilinear`, `micro_jog`, `short_chamfer`, `ninety_unfixable`, `detour`,
`long_detour`). Rip keys are connection keys `f"{net}:{a.ref}.{a.number}-{b.ref}.{b.number}"`
(pads ordered by `(ref, number)`) from `metrics.connections` (B-C7); a defect that cannot be
attributed to one connection rips every connection of that net on that layer.
`replace` semantics: merging N tracks emits `(first kiid, (merged,))` and `(other kiid, ())`
(empty = delete); a chamfer emits `(kiid_in, (shortened_in,))`, `(kiid_out, (shortened_out,
diagonal))`; new `Track`s carry `kiid=""`, the net, layer, width of the corner's tracks and
`locked=False`.

Algorithm (only `kiids` are touched; other copper is obstacle only):
1. Drop zero-length tracks (< 0.001 mm) and exact duplicates (same net, layer, width,
   endpoints in either order); merge collinear same-net/layer/width tracks that share an
   endpoint with no third item (track, via, pad) at it.
2. Per `polylines(model, net, layer)` (B-C5) of the touched tracks run `analyse_bends(points,
   width, pad_ends=…)`. Every `ninety` vertex → chamfer: for c in descending candidates
   `(3w, 2.5w, 2w, 1.5w, CHAMFER_MIN(w))` filtered to `c ≥ CHAMFER_MIN(w)` (when `3w <
   CHAMFER_MIN(w)` the only candidate is `CHAMFER_MIN(w)`), offset `a = c/√2` along both
   legs; feasible when both legs are ≥ a long, the diagonal's inflated polygon (half width +
   the net's clearance from `net_classes` / `rules["min_clearance"]`) overlaps no pad, track,
   arc or via of another net (`geom.polygons_overlap`) and `analyse_bends` of the new polyline
   reports no new `short_chamfers`/`micro_jogs`; first feasible c wins (DRC re-check is P6b);
   none feasible → rip `ninety_unfixable`.
3. `acute`, `non_octilinear`, `micro_jogs`, `short_chamfers`, off-pad `t_junctions`
   (`analyse_tracks`) on touched tracks → rip with the reason.
4. Per non-ground connection of a touched net: `routed_length / octi_dist` > 2.0 **and**
   extra > 5 mm (M5 hard) → rip `detour`; routed length > 50 % of the board diagonal (M6)
   **and** ratio > 1.5 (M5 review) → rip `long_detour` (plan call: a straight long
   connection cannot be improved by re-routing, so M6 alone never rips).
Deterministic: inputs sorted by KIID-free keys.

- [ ] **Step 1: Failing unit tests** (`test_chamfer.py`, P1a builders, spec §18.1):
  `test_ninety_corner_chamfered` — one L of two 5 mm tracks, width 0.2 mm, free space → one
  diagonal of length `0.6 ± 1e-6` (= 3 × w) at 45°, legs shortened by `0.6/√2`, `chamfered ==
  1`, output polyline: `analyse_bends(...)` has `ninety == acute == micro_jogs ==
  short_chamfers == ()` and `non_octilinear == ()`; `test_chamfer_shrinks_near_obstacle`
  (a pad of another net 0.35 mm outside the corner → c = 2w or the next feasible candidate,
  asserted exactly); `test_chamfer_min_when_3w_too_small` (w = 0.05 → c = 0.25);
  `test_chamfer_blocked_rips` (obstacle at the corner → rip key with reason
  `ninety_unfixable`, no replacement); `test_short_leg_rips` (legs 0.2 mm, w = 0.2);
  `test_acute_detected` (135° change → rip `acute`); `test_non_octilinear_detected` (a 30°
  segment → `non_octilinear`); `test_micro_jog_detected` (opposite-sense 45° pair 0.3 mm apart,
  w = 0.2 < `JOG_MIN` 0.4 → `micro_jog`); `test_short_chamfer_detected` (same-sense pair with
  0.15 mm straight piece, w = 0.2 → `short_chamfer`); `test_valid_chamfer_untouched` (pair
  with 0.25 mm piece → nothing); `test_merge_collinear` (3 collinear segments → one track,
  `merged == 2`); `test_merge_stops_at_via`; `test_zero_length_and_duplicate_dropped`;
  `test_detour_rips` (2-pad net, 10 mm direct, routed 26 mm → `detour`); `test_long_straight_not_ripped`
  (> 50 % diagonal, ratio 1.05 → no rip); `test_only_given_kiids_touched` (a 90° corner on
  a track outside `kiids` stays); `test_pad_entry_not_chamfered` (corner exactly at a pad
  centre treated as pad entry per `pad_ends`); `test_output_passes_analyse_bends` — seeded
  `random.Random(seed)` over 200 random rectilinear polylines in an empty board: after applying
  `replace`, every non-ripped polyline has M4 = M7 = 0 by `analyse_bends` (seed printed on
  failure); `test_deterministic`; `test_no_kipy_import`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/board/test_chamfer.py -q` — FAIL.
  **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): Freerouting post-processor (merge, chamfer, rip list)`.

### Task 5: Docs + index as built, checks, PR

- [ ] Index "As built (P6a)": lock values (URLs, hashes, sizes, Temurin version), image size
  delta, `NO_NETWORK_OPTIONS` exact names + settings-file path, passes option name confirmed,
  `unshare` availability in CI and in the app image (`docker run --user 1000:1000`),
  `run_freerouting(..., extra_args)` and `PostResult.reasons` (update B-C12/B-C13 in place),
  `scripts/app-image-tools.sh` added to the P6a ownership row. Residuals unchanged. No
  CHANGELOG entry (no tool yet; the image grows — one line under `[Unreleased]` "App image
  ships Freerouting 2.5.0 + Temurin 25 JRE (≈ +N MB) for the upcoming board fill routing").
- [ ] Run `uv run pytest -m "not kicad"`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy src`, the runtime tests with the fetched runtime, `bash
  scripts/app-image-tools.sh` on a local image (all with `timeout`).
- [ ] PR `board phase P6a: Freerouting runtime and post-processor`; review-loop per the index's
  Phase Conventions.

## Acceptance (P6a)

- [ ] CI `backend`, all 8 `kicad (…, i/8)` shards (each with the cache + fetch step) and `e2e`
  (`app-image-tools.sh` incl. Java 25 + JAR + `find_runtime`) green.
- [ ] `test_jar_hash_mismatch_refused`, `test_jre_hash_mismatch_refused` pass.
- [ ] `test_jvm_starts_under_limits`, `test_cancel_kills_no_zombie`, `test_no_network_attempt`
  pass in CI (no skip: `freerouting_rt` fails under `CI=true` without the runtime).
- [ ] `test_ninety_corner_chamfered`, `test_acute_detected`, `test_micro_jog_detected`,
  `test_short_chamfer_detected`, `test_merge_collinear`, `test_output_passes_analyse_bends` pass.
- [ ] Image size delta, lock values and the no-network mode recorded in the index.
