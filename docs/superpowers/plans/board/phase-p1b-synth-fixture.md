# Phase P1b — `sp4_synth` fixture: a KiCad-generated SMD schematic for the board phases

> Index contracts: none produced in code; provides the `synth_session` fixture and the
> golden netlist summary every later phase tests on. Spec §18.2 (fixture), §21 ("the
> `sp4_synth` fixture design and generator script"), Global Constraints "Fixtures are
> KiCad-generated". **Wave 1, parallel to P1a** — touch only the P1b row of the index's
> file-ownership table plus new files under `backend/tests/kicad/board/` named `*sp4_synth*`.

**Goal:** a reproducible ≈ 25-part, two-sheet SMD schematic built from KiCad's own empty
project through CopperChat's real schematic tools, ERC-clean, every symbol with a stock
footprint, **without a board** (P2's sync creates it), plus a committed golden summary of its
netlist that CI regenerates and compares.

`cc/` = `backend/src/copperchat/`.

**Files:**
- Create: `scripts/make-sp4-synth.py`, `backend/tests/kicad/board/{__init__,sp4_synth_steps,test_sp4_synth_build,test_sp4_synth}.py`,
  `backend/tests/kicad/board/goldens/sp4_synth_summary.json`,
  `backend/tests/fixtures/projects/sp4_synth/{sp4_synth.kicad_pro,sp4_synth.kicad_sch,power.kicad_sch}`
  (written by the generator only)
- Modify: `scripts/refresh-fixtures.sh` (call the generator), `backend/tests/support/kicad_project.py`
  (`synth_session`)
- Tests: as above (`-m kicad`), plus `backend/tests/unit/test_sp4_synth_summary.py` (pure
  summary builder on a recorded netlist string)

**Decision:** no board is committed, so there is exactly one fixture,
`synth_session` = the schematic-only `sp4_synth` project (`pcb_file is None`). The index's
`synth_nopcb_session` is dropped; tests that need a board create it with P2's sync.

---

### Task 1: The circuit as tool steps

**Interfaces:** `sp4_synth_steps.py`: `ROOT_STEPS: list[Step]`, `POWER_STEPS: list[Step]`
in the `(tool, args[, alias])` format of `tests/kicad/edit/test_scenarios.py` (executed by its
`run_script`), and `EXPECTED_PARTS: dict[str, tuple[str, str]]` (alias → (lib_id,
footprint)). Only stock symbols/footprints of the image libraries (all lib ids below were
checked in `ghcr.io/peterus/copperchat-kicad-base:52b43fb8…-r98c78374f0daab9d`). Passives
`R_0603_1608Metric` / `C_0603_1608Metric`, bulk caps `C_0805_2012Metric`.

| Sheet | Ref (alias) | Symbol | Footprint | Value / role |
|---|---|---|---|---|
| root | U1 | `MCU_ST_STM32F0:STM32F042F6Px` | `Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm` | MCU-like IC (USB on PA11/PA12) |
| root | C1, C2 | `Device:C` | 0603 | 100nF on VDD, VDDA (decoupling) |
| root | C3 | `Device:C` | 0805 | 4.7uF bulk on +3V3 |
| root | C6 | `Device:C` | 0603 | 100nF NRST to GND |
| root | R1 | `Device:R` | 0603 | 10k BOOT0 pin to GND |
| root | Y1 | `Device:Crystal_GND24` | `Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm` | 8MHz on PF0/PF1, pins 2/4 to GND |
| root | C4, C5 | `Device:C` | 0603 | 12pF load caps |
| root | J1 | `Connector:USB_C_Receptacle_USB2.0_16P` | `Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12` | VBUS, D+/D−, CC1/CC2, shield → GND, SBU no-connect |
| root | R2, R3 | `Device:R` | 0603 | 5.1k CC1/CC2 to GND |
| root | U3 | `Power_Protection:USBLC6-2SC6` | `Package_TO_SOT_SMD:SOT-23-6` | ESD on `USB_DP`/`USB_DN`, VBUS, GND |
| root | D1, R4 | `Device:LED`, `Device:R` | `LED_SMD:LED_0603_1608Metric`, 0603 | status LED on PA4, 1k |
| root | J2 | `Connector_Generic:Conn_01x04` | `Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical` | +3V3, SWDIO (PA13), SWCLK (PA14), GND |
| root | H1, H2 | `Mechanical:MountingHole` | `MountingHole:MountingHole_2.2mm_M2` | schematic mounting holes (P2 "moved, not duplicated") |
| power | U2 | `Regulator_Switching:TPS563200` | `Package_TO_SOT_SMD:SOT-23-6` | buck VBUS → +3V3, EN tied to VIN |
| power | L1 | `Device:L` | `Inductor_SMD:L_Changjiang_FNR4030S` | 2.2uH on SW |
| power | C7 / C8, C9 | `Device:C` | 0805 | 10uF in / 22uF out |
| power | C10 | `Device:C` | 0603 | 100nF bootstrap VBST–SW |
| power | R5, R6 | `Device:R` | 0603 | 33.2k / 10k FB divider |

26 parts, nets: `VBUS`, `+3V3`, `GND` (power symbols, `PWR_FLAG` on `VBUS`, `GND`, `+3V3`),
labels `USB_DP`, `USB_DN` (the diff pair J1 → U3 → U1), `SWDIO`, `SWCLK`, `OSC_IN`,
`OSC_OUT`, `SW`, `FB`; every unused MCU pin gets `sch_no_connect`. The `power.kicad_sch`
sheet is added with `sch_add_sheet {"name": "Power", "file": "power.kicad_sch"}`; it
connects through the global power nets only (no sheet pins needed). Placement coordinates
are on the 1.27 mm grid; use `sch_find_space` where a fixed coordinate collides.

- [ ] **Step 1: Failing unit test** (`test_sp4_synth_summary.py`): `test_steps_consistent` —
  every alias used in a `{X}.pin` string is defined earlier; every `sch_place_symbol` passes an
  explicit `footprint`; the set of lib ids equals the table; 24 ≤ part count ≤ 28.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_sp4_synth_summary.py -q` — FAIL.
  **Step 3: Implement** the step lists. **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `test(board): sp4_synth circuit as tool steps`.

### Task 2: Netlist summary (what "deterministic" means)

**Interfaces:** in `test_sp4_synth_build.py` (pure helpers, unit-tested):
`netlist_summary(xml_root) -> dict` and `erc_summary(erc_json: dict) -> dict`. The summary is
the comparison key between two generator runs and the committed golden:

```json
{"parts": {"U1": {"value": "STM32F042F6Px", "footprint": "Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm",
           "lib_id": "MCU_ST_STM32F0:STM32F042F6Px", "sheet": "/"}, "...": {}},
 "nets": {"GND": ["C1.2", "..."], "USB_DP": ["J1.A6", "..."]},
 "sheets": ["/", "/Power/"],
 "erc": {"errors": 0, "warnings": {"<type>": 1}}}
```

`nets` maps each net name to its sorted `"REF.pin"` list; nets with a single node are kept
(they reveal a dangling pin). **Not compared:** UUIDs, timestamps, KiCad's `(tstamps …)`,
symbol positions, file bytes. ERC warnings are compared by type and count; any type present
must be listed under "Accepted ERC items" in the generator's docstring with a reason (target:
none).

- [ ] **Step 1: Failing unit tests** (`test_sp4_synth_summary.py`, a small recorded
  `kicadxml` string and an ERC JSON string inline): `test_summary_ignores_uuids` (two
  netlists differing only in `tstamps`/`uuid` → equal summaries); `test_summary_sorted`;
  `test_erc_summary_counts_by_type`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `test(board): sp4_synth netlist/ERC summary`.

### Task 3: Generator test and script

**Interfaces:** `test_sp4_synth_build.py::test_build_sp4_synth` (`-m kicad`):
1. `create_empty_project(tmp, "sp4_synth")` — port of `scripts/make-blank-fixture.py`: KiCad's
   template `/usr/share/kicad/template/kicad.kicad_pro` copied verbatim as
   `sp4_synth.kicad_pro`; `sp4_synth.kicad_sch` created by a standalone `kicad-cli
   api-server` (`CreateDocument(DOCTYPE_SCHEMATIC)` + save); no `.kicad_pcb`.
2. Open a real `KicadSession` on it (`KicadSessionManager`, `real_settings`), an editing
   context with `change_budget=500` (as `blank_ctx`), run `ROOT_STEPS` then `POWER_STEPS`
   with `run_script`, `save("sch")`.
3. `kicad-cli sch erc --format json` and `kicad-cli sch export netlist --format kicadxml`
   (via `tests/support/kicad_cli.cli_netlist`) on the saved files → `summary`.
4. `assert summary == json.loads(GOLDEN.read_text())` unless `UPDATE_GOLDENS=1` (then write
   it, sorted keys, indent 1).
5. `COPPERCHAT_SP4_SYNTH_OUT` set: copy `sp4_synth.kicad_pro`, `sp4_synth.kicad_sch`,
   `power.kicad_sch` **verbatim** (`shutil.copy2`, KiCad-written) to
   `$OUT/sp4_synth/`; never `.kicad_prl`, `.lck`, `*-backups`.

`scripts/make-sp4-synth.py [FIXTURES_DIR]` (like `scripts/make-edited-fixtures.py`): runs
`uv run pytest -q -m kicad tests/kicad/board/test_sp4_synth_build.py` in `backend/` with
`COPPERCHAT_SP4_SYNTH_OUT=<dir>`, `COPPERCHAT_REQUIRE_KICAD=1`, `UPDATE_GOLDENS` passed through,
`timeout=1800`; exit code = pytest's. Its docstring lists the accepted ERC items (none
expected) and says: run inside the KiCad base image with the backend environment.
`refresh-fixtures.sh`: after the `make-blank-fixture.py` line,
`python3 "$ROOT/scripts/make-sp4-synth.py" "$DEST"` (needs `uv`; the comment says so).

- [ ] **Step 1: Failing kicad test** `test_build_sp4_synth` (golden file still absent → FAIL
  with a message naming `UPDATE_GOLDENS=1`).
- [ ] **Step 2: Implement** `create_empty_project`, the build, the copy-out and the script;
  iterate on `sp4_synth_steps.py` until ERC reports 0 errors and 0 warnings (fix the steps,
  never the files).
- [ ] **Step 3: Generate** `UPDATE_GOLDENS=1 python3 scripts/make-sp4-synth.py` in the
  kicad-base image → fixture files + golden written. **Determinism check:** run the
  generator a second time into a temp dir with `UPDATE_GOLDENS` unset → the test passes
  (summary identical); record both runtimes in "As built (P1b)".
- [ ] **Step 4: Run** `uv run pytest -m kicad tests/kicad/board/test_sp4_synth_build.py` —
  PASS. `git status` shows only the three fixture files, the golden and code.
- [ ] **Step 5: Commit** `test(board): sp4_synth fixture generated through the schematic tools`
  (fixture files in the same commit, message body: "generated by scripts/make-sp4-synth.py,
  KiCad <KICAD_COMMIT>").

### Task 4: `synth_session` and fixture tests

**Interfaces:** `tests/support/kicad_project.py`: `synth_session` (async fixture, `_open(...,
"sp4_synth", "sp4_synth")`, docstring "schematic only: no board is committed; P2's sync
creates it").

- [ ] **Step 1: Failing kicad tests** (`test_sp4_synth.py`, `synth_session`):
  - `test_opens_schematic_only`: `project.pcb_file is None`, `HistoryService.docs_of(project)
    == {"sch"}`, the session answers `project_info`.
  - `test_erc_clean`: the `run_erc` tool through `call_tool` → 0 errors; warnings only of the
    types the generator docstring accepts (target: none).
  - `test_netlist_classes`: `kicad-cli` netlist of a `save_copy` contains: one part with
    ≥ 20 pins whose sheet is `/` (MCU-like); a `Regulator_Switching` part and an inductor on
    `/Power/`; a crystal with two capacitors on its two non-GND pins; a USB-C connector whose
    `USB_DP`/`USB_DN` nets also reach a `Power_Protection` part and the MCU; an LED; a pin
    header; ≥ 3 capacitors sharing `+3V3` and `GND` with the MCU; parts on exactly two sheets.
  - `test_every_symbol_has_existing_footprint`: every component has a `footprint`, and
    `/usr/share/kicad/footprints/<lib>.pretty/<name>.kicad_mod` exists for each.
  - `test_netlist_imports_cleanly`: on a temporary copy, `CreateDocument(DOCTYPE_PCB)` +
    save, `OpenDocument`, `Board.import_netlist(<kicadsexpr netlist>, dry_run=True)` →
    `error_count == 0`, `warning_count == 0` (no pin/pad mismatch: P2's sync report must be
    clean on this fixture), new footprint count == number of parts.
  - `test_matches_golden`: the summary of the committed files equals
    `goldens/sp4_synth_summary.json` (catches a fixture edited after generation).
- [ ] **Step 2: Run** — FAIL (fixture missing). **Step 3: Implement** the fixture function.
  **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `test(board): synth_session fixture and fixture checks`.

### Task 5: Docs + index as built, checks, PR

- [ ] Index "As built (P1b)": final part/net counts, ERC result, generator runtime (two runs),
  the decision "one fixture `synth_session`, no `synth_nopcb_session`" (fix Phase
  Conventions "Real-KiCad tests" in place), `refresh-fixtures.sh` needing `uv`. No CHANGELOG
  entry (test-only).
- [ ] Run `uv run pytest -m "not kicad"`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy src`, and `uv run pytest -m kicad tests/kicad/board` in the kicad-base image
  (with `timeout`). Check `tests/unit/test_sharding.py` still passes (new kicad tests only
  add node ids).
- [ ] PR `board phase P1b: sp4_synth fixture`; review-loop per the index's Phase Conventions.

## Acceptance (P1b)

- [ ] CI `backend` and all 8 `kicad (…, i/8)` shards green, incl. `test_build_sp4_synth`
  (regenerates from scratch and matches the golden) and every test of `test_sp4_synth.py`.
- [ ] Fixture: 24–28 parts on 2 sheets, ERC 0 errors and no warning outside the generator's
  documented accepted list (target: none), every footprint a stock
  file, netlist dry-run import with 0 errors / 0 warnings, no `.kicad_pcb` committed.
- [ ] `refresh-fixtures.sh` regenerates `sp4_synth` and leaves `git diff` empty except
  UUIDs (summary unchanged).
- [ ] Index updated (`synth_session` only; As built P1b).
