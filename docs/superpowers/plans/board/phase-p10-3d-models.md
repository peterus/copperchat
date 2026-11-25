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
FetchReport(fetched, cached, missing, bytes)`: for each `.step` (fallback `.wrl` for renders)
not in the cache, GET
`https://gitlab.com/kicad/libraries/kicad-packages3D/-/raw/<sha>/<ref>` (read-only download;
nothing is posted anywhere), size cap 50 MB per file and 500 MB per call, timeout 60 s per
file, written to `<cache>/<ref>.part` then renamed; paths validated to stay inside the cache
(no `..`, no symlinks); a missing model is reported, never fatal.

- [ ] **Step 1: Failing unit tests:** `test_refs_from_model`, `test_path_traversal_refused`
  (`../x.step`), `test_cached_not_refetched` (fake HTTP via `httpx.MockTransport`),
  `test_size_cap`, `test_partial_download_not_cached`, `test_flag_off_no_fetch`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): on-demand KiCad 3D model cache pinned to the library version`.

### Task 2: STEP/GLB and 3D render with parts

**Interfaces:** with `board_3d_models` on, `pcb_export_3d` and `render_board(view="3d")` call
`ensure_models` first and pass `-D KICAD10_3DMODEL_DIR=<cache>` to `kicad-cli`; the result
lists `models: {fetched, cached, missing}` and replaces `BARE_BOARD_NOTE` with "parts from
KiCad 3D library <sha8>; missing: …". Flag off → P8 behaviour unchanged.

- [ ] **Step 1: Failing kicad tests** (`test_export_3d_models.py`, synth board after
  placement; needs outbound HTTPS, so it is opt-in with `COPPERCHAT_3D_MODELS_TEST=1` and
  skips with that reason otherwise — CI keeps the offline unit tests of Task 1):
  `test_step_contains_parts` (STEP has more `MANIFOLD_SOLID_BREP` entities than the bare
  board export), `test_missing_model_reported`, `test_flag_off_bare_board`.
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
