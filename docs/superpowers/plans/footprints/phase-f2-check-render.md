# Phase F2 — `fp_check` rules and `fp_render`

> Index contracts: FP-C3 (consumed), FP-C4, FP-C5, FP-C9. Spec §7.4, §7.5, §8, §11, §16.1
> (render hardening), §16.2 (`fp_render`, stock check). Index author calls 3 (SVG name
> sidecar) and 6 (`source_missing` severity). Runs in **wave 2** (after F1; F3 may still
> run — stay in the F2 column of the file ownership table).

**Goal:** check any footprint against the symbol's pins, a datasheet land pattern and
fixed manufacturability rules; render it with a mm grid as PNG (for the model and chat)
and as a zoomable, sandboxed SVG link.

**Files:**
- Create: `backend/src/copperchat/kicad_tools/footprints/{geom,checks}.py`,
  `backend/src/copperchat/kicad_tools/cli.py`, `backend/src/copperchat/views/footprint_render.py`
- Modify: `kicad_tools/checks.py` (use `run_kicad_cli`), `kicad_tools/footprints/specs.py`
  (+ `fp_check`, `fp_render`), `kicad_tools/footprints/read.py` (shared symbol-pin
  helper), `agent/profiles.py` (`FP_READ` += `fp_check`, `fp_render`),
  `web/routes_views.py` (renders route `.png|.svg`), `backend/pyproject.toml` +
  `backend/uv.lock` (`cairosvg>=2.7.0`)
- Tests: `backend/tests/unit/footprints/{test_geom,test_checks,test_check_tool,test_render_wrapper}.py`,
  `backend/tests/unit/test_routes_views*.py` (existing renders route tests),
  `backend/tests/unit/test_kicad_cli.py`, `backend/tests/kicad/footprints/{test_fp_check,test_fp_render}.py`

**Not in F2:** recording renders/checks in `footprint_reviews` (F4b adds that hook);
journal-based `pending` (F4a wires it into `CheckInput.pending`).

---

### Task 1: Geometry primitives

**Interfaces:** Produces in `geom.py`: `pad_polygon(pad: Pad) -> list[tuple[float, float]]`
(rect, roundrect, oval, trapezoid as exact polygons; arcs as 16-segment chords per quarter;
circle → `("circle", center, r)` handled separately), `stroke_segments(shape: Shape) ->
list[Seg]` (centre lines; arcs/circles as chords, half width carried),
`distance(a, b) -> float` (polygon/polygon, polygon/segment, circle cases; 0 when they
overlap or one contains the other), `point_in_polygon(p, poly) -> bool` (boundary =
inside), `closed_outline(shapes) -> list[tuple[float, float]] | None` (a rect, a polygon,
or segments/arcs whose endpoints form one loop within 1 µm), `candidate_pairs(boxes,
gap) -> Iterator[tuple[int, int]]` (sort-and-sweep on x, then y overlap of boxes grown by
`gap`).

- [ ] **Step 1: Failing tests** (`test_geom.py`): rect pad 1.3×3.5 at (−1.1, 0) → 4
  vertices ±0.65/±1.75 around the centre; rotated 90° → swapped; roundrect ratio 0.25
  corner radius = 0.25 × min(w, h); `distance` of the L201 pads = 2.2 − 1.3 = 0.9;
  overlapping → 0.0; a closed 4-segment loop → polygon, a 3-segment open path → None;
  `candidate_pairs` on 1,000 random boxes equals the brute-force pair set.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** (plain Python, no new library).
- [ ] **Step 4: Run** — PASS. **Step 5: Commit** `feat(footprints): geometry primitives`.

### Task 2: `run_checks` — every §8 rule

**Interfaces:** Consumes FP-C3, Task 1. Produces FP-C4 `run_checks`, `CheckInput`,
`Expected`, `Finding`, `CheckReport`, `to_result`.

Rules, thresholds and severities (spec §8, binding):

| Rule | Condition | Severity |
|---|---|---|
| `pin_without_pad` | symbol pin number without pad | error |
| `pad_without_pin` | numbered copper pad without pin | warning; info if in `allow_unmatched` |
| `pad_count_summary` | `n pins / m pads / k unnumbered` (only with pins) | info |
| `duplicate_pad_number` | several pads share a number | info |
| `expected_mismatch` | position/size/drill/pitch/courtyard/body off by > `tolerance_mm` (default 0.01) | error |
| `expected_missing_pad` | expected pad missing, or extra footprint pad | error |
| `courtyard_missing` | no closed F.CrtYd (B.CrtYd for bottom-only) | error |
| `courtyard_not_enclosing` | copper or F.Fab outside the courtyard | error |
| `courtyard_margin` | gap < 0.25 mm (< 0.1 mm if body or copper extent ≤ 1.0 mm) | warning |
| `fab_missing` | no F.Fab outline | warning |
| `silk_on_pad` | silk–copper clearance < 0.1 mm | warning |
| `silk_outside_courtyard` | silk outside the courtyard (Reference/Value field texts excluded: KiCad puts Reference outside the courtyard) | warning |
| `pin1_marker_missing` | > 2 pads, or 2 pads and `polarized`: no silk/fab mark nearer to pad 1 than to any other pad within `max(pad1.size) + 1.0` mm | warning |
| `mask_missing` | SMD pad without F.Mask | error |
| `paste_missing` | SMD pad without F.Paste | warning |
| `paste_on_tht` | THT pad with paste | warning |
| `mounting_mismatch` | `smd` with only THT pads or `tht` with only SMD pads | error |
| `pad_too_small` | a pad dimension < 0.15 mm | warning |
| `pad_gap_small` | gap between pads of different numbers < 0.15 (warning), < 0.1 (error) | warning / error |
| `drill_small` | drill < 0.2 (error), 0.2 ≤ d < 0.3 (warning) | error / warning |
| `annular_ring_small` | `(min(pad size) − max(drill)) / 2 < 0.15` | warning |
| `source_missing` | local footprint without `Footprint source` (spec §8; index call 6); n/a for non-local | error |
| `needs_user_review` | local and `review_state != "approved"` | info |
| `approved_geometry_changed` | `User review` present and its `geometry <hash8>` ≠ current hash[:8] | warning |
| `too_complex_partial_check` | > 2,000 pads or > 5,000 shapes: names the skipped pair rules (`pad_gap_small`, `silk_on_pad`) | info |

Silk texts count as boxes `len(value) × 0.8 × size_x` by `size_y`, centred at `at`,
rotated by the text rotation.

- [ ] **Step 1: Failing tests** (`test_checks.py`, synthetic `FootprintGeometry` built in
  code): one test per rule **and** severity branch (parametrised table), each asserting
  `rule`, `severity` and the finding's `pads`/`measured`/`expected`. Named cases:
  - `test_l201_target_passes`: the owner's numbers (pads 1.3×3.5 at x=±1.1, CrtYd 4×4, Fab
    3×3, silk outside pads, pins `["1","2"]`) → `passed`, 0 errors, 0 warnings.
  - `test_j402_shell_pads`: 9 pins, 11 pads → `pad_without_pin` warnings for `10`, `11`;
    with `allow_unmatched={"10","11"}` → info; `passed` in both.
  - `test_drc0010j_expected_mismatch`: 10 pads + EP `11`, `expected` with pad 3 shifted
    0.05 mm and EP 0.1 mm larger → two `expected_mismatch` errors.
  - `test_source_missing_severity`: local + no source → error (also for a seed copy without
    any custom field); global → absent.
  - `test_reference_text_outside_courtyard_ok`: Reference on F.SilkS 1 mm above the courtyard
    → no `silk_outside_courtyard`; a silk segment there → warning; the Reference text over a
    pad → `silk_on_pad`.
  - `test_findings_capped`: the 1,000-pad grid with 0.05 mm gaps → `counts` exact, at most 20
    findings per rule and 200 overall, `truncated: true`, `omitted` per rule.
  - `test_pending_counts_unreviewed`: both fields present, `pending=True` →
    `needs_user_review` info present.
  - `test_cost_bound`: 2,500-pad grid → `too_complex_partial_check` info, other rules
    still run, finishes < 2 s.
  - `test_pad_gap_uses_sweep`: 1,000-pad grid at 0.5 mm pitch finishes < 2 s and gives the
    same findings as a brute-force run on a 100-pad subset.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** `checks.py` (pure; no KiCad import).
- [ ] **Step 4: Run** — PASS. **Step 5: Commit** `feat(footprints): fp_check rules`.

### Task 3: `fp_check` tool

**Interfaces:** Produces tool `fp_check` (spec §7.4 input/output), helper
`symbol_pins(ctx, symbol: str) -> tuple[list[str], bool]` in `read.py` (pin numbers and
`polarized`; a `lib:Name` goes through `LibraryService.get_symbol`, a reference like `R201`
through the placed symbol's pins; `polarized` = exactly 2 pins whose names differ and are
neither `~` nor equal to their number).

- [ ] **Step 1: Failing unit tests** (`test_check_tool.py`): schema (`expected.pads.maxItems
  512`, `allow_unmatched_pads.maxItems 64` (items ≤ 16 chars), `symbol` ≤ 256, the numeric
  bounds of index call 18 on `expected` values, `tolerance_mm` 0.001…1.0, additionalProperties
  false at every level); `symbol` that is neither `lib:Name` nor `^[A-Za-z#]+[0-9]+$` →
  `invalid_argument`; the check runs via `asyncio.to_thread` (patch and assert called).
- [ ] **Step 2: Failing kicad tests** (`test_fp_check.py`, `blank_nopcb_session`):
  - `test_stock_with_symbol`: `fp_check("Inductor_SMD:L_Bourns-SRN4018", symbol="Device:L")`
    → `passed`, `pad_count_summary` info `2 pins / 2 pads / 0 unnumbered`.
  - `test_stock_expected_u202_pattern`: the stock DRC0010J/VSON-10 footprint
    (`Package_SON:Texas_DRC0010J_ThermalVias` or the current stock name — look it up with
    `lib_search_footprints("DRC0010J")` and pin it in the test) with an `expected` built from
    its own `fp_get` read-back → `passed`; the same with pad 1 moved 0.1 mm →
    `expected_mismatch`. The test stores the measured pad table in
    `tests/fixtures/golden/fp_check_DRC0010J.json` (CopperChat output) for the manual
    datasheet comparison (spec §16.3).
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement** the tool in `specs.py`/`read.py`; add
  `fp_check` to `FP_READ`.
- [ ] **Step 5: Run** — PASS. **Step 6: Commit** `feat(footprints): fp_check tool`.

### Task 4: Generic `run_kicad_cli`

**Interfaces:** Produces FP-C5 `run_kicad_cli`, `CliResult`. Consumes the isolation and
kill/reap code now inside `kicad_tools/checks.py` (`run_kicad_check_on_dir`).

- [ ] **Step 1: Failing unit tests** (`test_kicad_cli.py`, a fake `kicad-cli` script in
  `tmp_path` set through `settings.kicad_cli`): argv passed unchanged (an arg `--fp=-x`
  arrives as one element); env has the isolated `HOME`/config dir; a sleeping fake is
  killed after the timeout and reaped (no zombie: `os.waitpid` raises `ChildProcessError`),
  raising `TimeoutError`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** `kicad_tools/cli.py` by moving the
  plumbing out of `checks.py`; `run_kicad_check_on_dir` calls it.
- [ ] **Step 4: Run** — PASS, plus the existing ERC/DRC tests
  (`tests/kicad/test_checks.py`, `tests/kicad/edit/test_checks_real.py`) unchanged.
- [ ] **Step 5: Commit** `refactor: generic run_kicad_cli`.

### Task 5: Render pipeline

**Interfaces:** Produces FP-C5 `render_footprint`, plus pure helpers in
`views/footprint_render.py`: `auto_grid(extent_mm) -> float` (≤ 5 → 0.1, ≤ 20 → 0.25,
≤ 50 → 0.5, else 1.0), `load_kicad_svg(data: bytes) -> ET.Element` (refuses output > 10 MB and `<!DOCTYPE` /
`<!ENTITY` with `render_failed`; drops `script`, `style` and `foreignObject` elements, every `on*` attribute, any `href`
attribute in any namespace (matched on the local name of the Clark key, e.g.
`{http://www.w3.org/1999/xlink}href`) that is not a `#fragment`, and any `url(` not pointing
to `#…` in `style` attributes while re-serialising), `build_wrapper(kicad_svg: ET.Element, geometry,
grid_mm, labels) -> bytes`, `rasterise(svg: bytes, extent_mm) -> tuple[bytes, float]`
(scale = max(40, …) px/mm, longest side ≤ 2000 px, `cairosvg.svg2png(bytestring=…,
url_fetcher=_refuse, output_width=…)`, run via `asyncio.to_thread`). The **whole** render
(kicad-cli, parse, wrapper, rasterise) runs under the module `Semaphore(1)`.

- [ ] **Step 1: Failing unit tests** (`test_render_wrapper.py`):
  - `test_doctype_refused`, `test_entity_refused` → `render_failed`.
  - `test_url_fetcher_refuses`: an SVG with `<image href="http://127.0.0.1:1/x.png">` →
    `_refuse` was called and raised; no socket opened (patch `socket.socket` to fail).
  - `test_oversize_output_refused` (10 MB + 1 byte → `render_failed`) and
    `test_active_content_dropped` (`<script>`, `onload=`, `<foreignObject>`,
    `href="http://x"`, `xlink:href="http://x"`, `<style>`, `style="fill:url(http://x)"` are gone
    from the wrapper; `href="#a"` stays).
  - `test_wrapper_structure`: for viewBox `-2 -2 4 4` and grid 0.1 → minor lines every 0.1,
    major every 1.0, axis lines through 0, tick labels on two edges, nested `<svg>` with the
    same viewBox; a pad number `"<&>"` is escaped (parse the output with `ET.fromstring`).
  - `test_grid_coarsened_for_large_extent` (200 mm extent, `grid_mm=0.1` → ≤ 500 lines per
    axis); `test_bad_dir_or_name_refused` (relative dir, dir not ending in `.pretty`, name with
    `/` or `..` → `render_failed`/`invalid_argument` without spawning kicad-cli).
  - `test_major_every_5mm_above_20`: extent 30 mm → major spacing 5.
  - `test_raster_size`: 4×4 mm → width ≥ 160 px; 100×10 mm → longest side 2000 px.
  - `test_missing_or_empty_output`: fake `run_kicad_cli` returns 0 but writes nothing / an
    empty file → `render_failed`; the temp dir is removed in both cases.
  - `test_one_render_at_a_time`: two concurrent `render_footprint` calls with a slow fake →
    the second starts after the first ended (module `asyncio.Semaphore(1)`).
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement**: library directory = `GetLibraryTable(…, substituted)` row of
  the nickname (project scope first, then global), which must be an absolute, existing
  directory ending in `.pretty`; the name must not contain `/`, `\\`, NUL or `..`; and
  `footprint_geometry(lib_id)` must succeed first (`not_found` stops before kicad-cli);
  `build_wrapper` coarsens an explicit `grid_mm` so that no axis gets more than 500 grid
  lines; `kicad-cli fp export svg <dir> --fp=<name>
  --layers <csv> -o <mkdtemp>` with timeout 60 s; default layers F.Cu, F.Mask, F.Paste,
  F.SilkS, F.Fab, F.CrtYd plus each B.* layer the geometry uses; dimension labels (pad-1
  size and centre, pitch between pad 1 and the next number, courtyard size) taken from the
  `FootprintGeometry` read-back, never from tool input.
- [ ] **Step 4: Run** — PASS. **Step 5: Commit** `feat(footprints): render pipeline`.

### Task 6: `fp_render` tool, SVG route, dependency

**Interfaces:** Produces tool `fp_render` (spec §7.5: `{lib_id, layers? (maxItems 32, each matching
`^(F|B)\.(Cu|Mask|Paste|SilkS|Fab|CrtYd)$` or `^Edge\.Cuts$`), grid_mm? ∈ {0.1, 0.25, 0.5,
1.0}, labels?}` → `{image, svg, scale_px_per_mm, extent_mm, geometry_hash}` +
one PNG `ImageBlock`); files `.kicad-agent/renders/<uuid32>.png`, `<uuid32>.svg`,
`<uuid32>.svg.json` (`{"download_name": "<lib>_<name>.svg"}`, index call 3).

- [ ] **Step 1: Failing route tests** (existing renders route test module):
  `test_png_unchanged` (same headers as before); `test_svg_headers` (`Content-Type:
  image/svg+xml`, the exact CSP string, `nosniff`, `Content-Disposition: inline;
  filename="OpenDriveHub_L_Bourns_SRN3012BTA.svg"` from the sidecar);
  `test_svg_name_sanitised` (sidecar `"../a b\".svg"` → only `[A-Za-z0-9_.+-]`, ≤ 120,
  ends `.svg`); `test_svg_without_sidecar` → `<uuid>.svg`; `test_json_not_served` (404 for
  `<uuid>.svg.json`); `test_non_uuid_rejected`.
- [ ] **Step 2: Failing kicad tests** (`test_fp_render.py`, `blank_nopcb_session`):
  - `test_render_seed`: PNG exists and decodes; result `geometry_hash` equals `fp_get`'s.
  - `test_crtyd_extent_matches_geometry`: `layers=["F.CrtYd"]` → the KiCad SVG viewBox size
    equals `fp_get`'s `graphics["F.CrtYd"].bbox` size within 0.01 mm (full exports include
    texts and are not asserted, spec §16.2).
  - `test_cairosvg_in_image`: `import cairosvg; cairosvg.__version__ >= "2.7.0"` and a 1×1 mm
    SVG rasterises (verifies `libcairo2` in the base image).
- [ ] **Step 3: Run** — FAIL.
- [ ] **Step 4: Implement**: `uv add 'cairosvg>=2.7.0'` (lockfile updated); tool in `specs.py`
  (+ `FP_READ`); route pattern `^[0-9a-f]{32}\.(png|svg)$`, dispatch by suffix. Check
  that the app image is built from the kicad base (`docker/app/Dockerfile`); if it installs
  Python deps separately, `libcairo2` must be present there too — add the test above to
  the image smoke test if needed.
- [ ] **Step 5: Run** — PASS. **Step 6: Commit** `feat(footprints): fp_render and SVG route`.

---

**Acceptance (F2 done when):** every §8 rule has a unit test per severity; J402 and
DRC0010J cases pass; `fp_render` PNG + SVG work in the kicad image; renders route
serves PNG unchanged and SVG with the CSP headers; ERC/DRC tests unchanged; CI green.

**Risks:** CairoSVG/libcairo mismatch in the image (Task 6 test); KiCad SVG markup that
`xml.etree` cannot parse (refuse with `render_failed`, never fall back to string
concatenation); text boxes are approximations (silk warnings only, never errors).

**Do NOT:** add shapely, defusedxml or any other new library besides `cairosvg`; build
SVG by string concatenation; serve `.json` sidecars; call `cairosvg` with `unsafe=True`;
write any KiCad file; touch `history/` (F3); act on KiCad GitLab; request Copilot.
