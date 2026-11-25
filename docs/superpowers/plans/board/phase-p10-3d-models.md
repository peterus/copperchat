# Phase P10 (optional) — 3D part models on demand, STEP with parts

> Index contracts: B-C9 (`render_board` view `3d`), P8's `pcb_export_3d` and
> `BARE_BOARD_NOTE`, `cc/board/paths.py`. Spec §15.3 D3, §16 G7. **Starts only after the
> owner's explicit OK** (D3: deferred, optional); depends on P8. Not part of the SP4 release.

**Goal:** STEP/GLB exports and 3D renders carry the real KiCad 3D models of the footprints in
use, fetched on demand into a cache pinned to the KiCad library version — without baking
≈ GBs of models into the image. Case fit stays the owner's review against the repo's CAD (D3).

`cc/` = `backend/src/copperchat/`.

**Verified while planning:** `docker/kicad-base/LIBS_COMMITS` pins only
`KICAD_SYMBOLS_COMMIT` and `KICAD_FOOTPRINTS_COMMIT` (gitlab `kicad/libraries/kicad-symbols` /
`kicad-footprints`, built in the base image's `libs` stage, "no 3D models"); there is no
3D-models pin. The pinned footprints reference models as `${KICAD10_3DMODEL_DIR}/<lib>.3dshapes/<name>.step`
(≈ 14.9 k references in `/usr/share/kicad/footprints` of the pinned base image); `kicad-cli pcb
export step|glb` accepts `-D/--define-var` to set that variable. `LIBS_COMMITS` is part of the
kicad-base recipe hash (`scripts/kicad-base-image.sh`), so P10 **does not** edit it (that
would rebuild KiCad).

**Files:**
- Create: `docker/kicad-3dmodels.lock` (`KICAD_PACKAGES3D_COMMIT=<sha>` — the
  `kicad/libraries/kicad-packages3D` commit closest to, not after, the
  `KICAD_FOOTPRINTS_COMMIT` date; the executor records both dates), `cc/board/models3d.py`
  (pure: model references of a model → cache paths), `cc/kicad_tools/board/models3d.py`
  (fetcher)
- Modify: `cc/config.py` (append `board_3d_models: bool = Field(default=False,
  validation_alias="COPPERCHAT_BOARD_3D_MODELS")`, cache dir `<data_dir>/kicad-3dmodels/<sha>/`),
  `cc/kicad_tools/board/fab.py` (`pcb_export_3d`), `cc/views/board_render.py` (`3d` view),
  `README.md`, `.env.example`, `CHANGELOG.md`
- Tests: `backend/tests/unit/board/test_models3d.py`, `backend/tests/kicad/board/test_export_3d_models.py`

---

### Task 1: Model list and on-demand cache

**Interfaces:** `model_refs(model: BoardModel) -> tuple[str, ...]` (the `${KICAD10_3DMODEL_DIR}`-relative
paths of the footprints in use; `BoardModel` gains nothing — the loader already reads
footprints; P10 adds a `models: tuple[str, ...]` field to `Part` only if the loader does not
expose them, recorded as a B-C2 deviation); `async def ensure_models(settings, refs) ->
FetchReport(fetched, cached, missing, bytes, evicted)`: for each `.step` (fallback `.wrl` for renders)
not in the cache, GET
`https://gitlab.com/kicad/libraries/kicad-packages3D/-/raw/<sha>/<ref>` (read-only download;
nothing is posted anywhere), size cap 50 MB per file and 500 MB per call, timeout 60 s per
file (`MODEL_FILE_TIMEOUT_S`) and a **total deadline** `MODEL_FETCH_DEADLINE_S = 300` per
call (each file's timeout is `min(60, remaining)`; refs not started by the deadline are
reported `missing` with reason `deadline`); paths validated to stay inside the cache (no `..`,
no symlinks); a missing model is reported, never fatal. **Cache writes:** each download goes
to a **unique** temp file from `tempfile.mkstemp(dir=<cache>/<ref dir>, prefix=".dl-",
suffix=".part")` (same filesystem, so the final `os.replace(tmp, <cache>/<ref>)` is atomic;
never a fixed `<ref>.part` name two writers could share); a **per-model file lock**
(`fcntl.flock` on `<cache>/.locks/<sha256(ref)[:32]>.lock`, taken in a thread, released in
`finally`) around "check cached → download → replace", so two calls (or two processes)
fetching the same model download it once and the second finds it cached. **Content check**
before the replace: non-empty, the ref's extension is `.step`/`.stp`/`.wrl`, and the first
bytes match it (`ISO-10303-21;` for STEP, `#VRML` for WRL; an HTML error page or a truncated
body fails) → else the temp file is deleted and the model reported `missing` with reason
`bad_content`. **Size cap of the whole cache** `MODEL_CACHE_MAX_BYTES = 2 GiB`: after a fetch
the cache is trimmed LRU (each use of a cached model touches its mtime; oldest mtime first;
models referenced by the current call are never evicted; temp/lock files ignored) until it is
≤ the cap; evictions are counted in `FetchReport.evicted`.

- [ ] **Step 1: Failing unit tests:** `test_refs_from_model`, `test_path_traversal_refused`
  (`../x.step`), `test_cached_not_refetched` (fake HTTP via `httpx.MockTransport`),
  `test_size_cap`, `test_partial_download_not_cached`, `test_flag_off_no_fetch`;
  `test_unique_temp_names` (spy on `mkstemp`: two downloads of different refs in the same dir
  get different temp names, both inside the cache dir); `test_concurrent_download_same_model`
  (two `ensure_models` calls with the same ref started together via `asyncio.gather`, a
  `MockTransport` counting requests and delaying the body → exactly 1 request, both reports
  have the file `fetched`/`cached`, the cached file equals the body, no `.dl-*.part` left);
  `test_bad_content_rejected` (an HTML body for a `.step` ref, and an empty body → `missing`
  with reason `bad_content`, nothing cached); `test_cache_lru_cap`
  (`MODEL_CACHE_MAX_BYTES` monkeypatched to 3 files' size, 4 fetches → the least recently
  used one evicted, a model of the current call never evicted, `evicted == 1`);
  `test_total_deadline_stalling_transport` (a `MockTransport` whose handler stalls longer than
  the per-file timeout for every request, `MODEL_FETCH_DEADLINE_S` monkeypatched to 2 s and
  the per-file timeout to 1 s, 5 refs → returns within 2 s + 1 s slack, unstarted refs
  `missing` with reason `deadline`, no temp file left).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): on-demand KiCad 3D model cache pinned to the library version`.

### Task 2: STEP/GLB and 3D render with parts

**Interfaces:** with `board_3d_models` on, `pcb_export_3d` and `render_board(view="3d")` call
`ensure_models` first — **before** taking the edit lock (`pcb_export_3d` holds
`ctx.editing.edit_lock` for the export, P8): the refs come from a `board_model` read outside
the lock, the fetch runs with its 300 s total deadline outside it, and under the lock the
refs are re-derived from the locked model and only looked up in the cache (a ref new since
the pre-lock read is reported `missing` with reason `changed`, never fetched under the
lock) — so a slow or stalling download never blocks board writes; then they pass
`-D KICAD10_3DMODEL_DIR=<cache>` to `kicad-cli`; the result
lists `models: {fetched, cached, missing}` and replaces `BARE_BOARD_NOTE` with "parts from
KiCad 3D library <sha8>; missing: …". Flag off → P8 behaviour unchanged.

- [ ] **Step 1: Failing kicad tests** (`test_export_3d_models.py`, synth board after
  placement; needs outbound HTTPS, so it is opt-in with `COPPERCHAT_3D_MODELS_TEST=1` and
  skips with that reason otherwise — CI keeps the offline unit tests of Task 1):
  `test_step_contains_parts` (STEP has more `MANIFOLD_SOLID_BREP` entities than the bare
  board export), `test_missing_model_reported`, `test_flag_off_bare_board`;
  `test_fetch_outside_edit_lock` (offline: `ensure_models` patched to a stalling coroutine →
  while it stalls, a concurrent `pcb_move` acquires the edit lock and completes; the export
  then takes the lock — runs without `COPPERCHAT_3D_MODELS_TEST`).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS; record
  download size and seconds for `sp4_synth`.
- [ ] **Step 5: Commit** `feat(board): STEP/GLB exports and 3D renders with part models`.

### Task 3: Docs + index as built, PR

- [ ] Index "As built (P10)", README row `COPPERCHAT_BOARD_3D_MODELS` (default off; outbound
  HTTPS to gitlab.com; cache size per board), `.env.example`, CHANGELOG `[Unreleased]`.
- [ ] Checks as every phase; PR `board phase P10: 3D models on demand`; review-loop.

## Acceptance (P10)

- [ ] Owner OK recorded in the PR before work starts.
- [ ] CI green; `test_step_contains_parts` passes in the opt-in run; flag off keeps P8's bare
  export byte-for-byte behaviour.
- [ ] Cache pinned to `KICAD_PACKAGES3D_COMMIT`; `LIBS_COMMITS` untouched.
