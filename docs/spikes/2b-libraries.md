# Spike 2B — library access via the KiCad API

## Question(s)
Sub-project 2 needs symbol/footprint search and inspection (`lib_search_*`, `lib_get_symbol`, spec §6.1)
and a footprint-package check for the `passives_package: 0805` part preference (§9.1), **without
parsing library files** (data only from the API). 1. Cost of `load_all_libraries` with the full stock
libraries. 2. What `GetLibraryItems` / `GetItemsFromLibrary` return (symbols and footprints).
3. Project libraries via project library tables with `${KIPRJMOD}`. 4. Does placing need
`load_all_libraries`? 5. In-memory search index: build time, latency (< 300 ms). 6. Can a footprint's
package (0805) be derived reliably?

## Setup
- KiCad `0616634034fa1a8c92c141c293342cbef2f68dff`, kipy `87b945a0`, image
  `ghcr.io/peterus/copperchat-kicad-base:0616634034…` (stock libs: 224 symbol libs / 22 916 symbols,
  156 footprint libs / 15 478 footprints). Headless `kicad-cli api-server`, schematic + board opened,
  `LANG=C`, spike-local `KICAD_CONFIG_HOME` seeded with KiCad's template library tables (as
  `launcher.seed_library_tables`). Host: 6 cores, 32 GB, busy with other jobs.
- Harness: `spikes/2b-libraries/` (docker; `./run_all.sh`). Source read at the pin:
  `common/api/api_handler_libraries.cpp`, `eeschema/api/api_handler_sch_libraries.cpp`,
  `eeschema/lib_symbol.cpp` (`Serialize`).

## Results
`./run_all.sh` → all 96 checks pass (checks that document a refusal pass when KiCad refuses). Wall-clock numbers vary between runs (host load); ranges below are over the runs made, `results/` holds the last run.

| # | Question | Result |
|---|---|---|
| 1 | `load_all_libraries` | call returns in 0.5 s, load finishes in **7–14 s** (varies with host load; last committed run 14.2 s; these loads ran with the schematic open only); `wait_for_libraries` works. RSS **221 MB → 4.9–5.0 GB** (symbols only 1.35 GB / 1.5–3.2 s; footprints only 3.8 GB / 7–8.4 s). All 380 libraries reach `LLS_LOADED` (asserted) |
| 2a | `GetLibraryItems` | **names only**: `(library_nickname, entry_name)`. No description, keywords, pin count. Listing all: 0.07 s (symbols) / 0.11 s (footprints) |
| 2b | `GetItemsFromLibrary` symbol | full definition: description, keywords, footprint filters, default footprint field, datasheet, unit count, body-style names, power type, fields, pins as children (number, name, electrical type, position relative to origin in nm, y down, per-child unit and body style). **Batchable**: 500 ids per request → all 22 916 symbols in **6.5 s** (1 id per request: 11 ms each → 4 min). Missing ids are silently omitted |
| 2c | Field coverage (of 22 916) | description 22 577, keywords 22 527, footprint filters 21 743, default footprint 18 160, multi-unit 1 344, multi-body-style 43, power 105 |
| 2d | **Derived symbols** (`extends`) | **12 313 of 22 916 (53.7 %) come back with no child items → no pins** (a plain `grep -c "(extends "` over the stock files, done once in the image only to cross-check, counts 12 324 — close but not identical, the 11 difference was not investigated; the proto has no parent reference). Their other fields are complete. **Workaround that stays API-only:** `place_symbol_from_library` inside `begin_commit` … `drop_commit` returns the instance with a flattened `definition` (e.g. 48 pins for STM32F103C8Tx); the document was unchanged and not modified afterwards (`is_document_modified` false), median **22 ms** per probe over 44 probes spread across 34 libraries (every 300th derived symbol plus STM32F103C8Tx → 48 pins and LM358 → 8 pins, asserted) |
| 2e | Footprint | `Footprint` proto: description, keywords, items (pads with number and SMD/THT type, graphics, 3D model). All 15 478 in **14–19 s** (Python unpack of pads dominates). Pad count/type derivable |
| 3 | Project libraries | `ecc83`, `cm5_minima`, `pic_programmer` (`${KIPRJMOD}/libs/…`, `${KICAD10_FOOTPRINT_DIR}` rows) and `interf_u` (legacy `$(KIPRJMOD)`, `Legacy` option): all project rows `LLS_LOADED`, listed with `get_library_statuses(scope=PROJECT)`, details and placement work. Project libs are **appended last** in the table order, so "project first" is CopperChat's job. Project data quality varies (empty descriptions) |
| 4 | Needs `load_all`? | `PlaceSymbolFromLibrary` does **not** load libraries by itself: cold place → "symbol not found". `GetItemsFromLibrary` and `GetLibraryItems(nickname)` **do** load that one library on demand (0.13 s; the whole cold sequence in S1 grew RSS 221 → 272 MB) and placing works afterwards. **Footprint** requests are refused ("no handler") on a server with only the schematic open, also after a symbols-only `load_all_libraries`; they work lazily per library once the board is open (S2), and after `load_all_libraries(footprints)` (which loads the PCB kiface, q01 `load_footprints_only` lists 15 478 with only the schematic open). |
| 5 | Index | pure-Python inverted index with prefix matching: build **1.1 s** (symbols) + **1.1 s** (footprints), 244 MB client heap (traced), query **p50 0.2 ms / p95 2.2–2.5 ms / max 7–9 ms** (symbols), **p50 0.8–1.0 / p95 7–9 / max 7–10 ms** (footprints) ≪ 300 ms. 20 symbol + 10 footprint queries return non-empty results and 11 of them (8 symbol, 3 footprint) have an asserted expected top-5 hit ("resistor"→`Device:R`, "LM358", "0805 resistor"→`R_0805_2012Metric`); the other top-3 lists were only eyeballed. Known quirks: "+3V3" also ranks `power:-3V3` second, "0603 capacitor" ranks `C_0201_0603Metric` (metric 0603) among the top hits — the index does no size-aware matching. Name-only index (right after the listing, 0.6 s build): finds "LM358"/"led"/"GND" but **not** "resistor"/"capacitor" (names `R`, `C`) |
| 5b | Warm-up timeline | api load 7 s + list 0.2 s + symbol details 6.5 s (18 s under `tracemalloc`, which the q05 timeline used) + footprint details 14–19 s + index 2 s ≈ **30–46 s** end-to-end, doable in the background; symbol search is usable after ~15 s |
| 6 | Package derivation | `package_of(name)`: regex `<imperial>_<metric>Metric` accepted only when the pair is consistent (0805↔2012), so `C_01005_0402Metric` is not read as 0402. In the five `*_SMD` libs it finds the common chip sizes; of the 115 footprints whose name yields a package and have a description, 113 agree with the description; the 2 that do not (below) are the only ones without exactly 2 SMD pads, so with the extra rule *2 SMD pads* (113 footprints) the name and description always agree; the 2 false positives without it (`L_…_Array_1206_3216Metric`, `RF_Converter:Anaren_0805_2012Metric-6`). Not derivable: non-chip/odd names (`D_2114_3652Metric`, `L_Coilcraft_0805HQ…` is derivable only by token) → "unknown", never a deviation |

## Findings
1. **Listing is names only**; every searchable attribute needs `GetItemsFromLibrary`, which is cheap
   when batched (500/request: 6.5 s for all symbols).
2. **Memory is the real cost:** ~4.9 GB RSS per api-server with all stock libraries (3.8 GB of it
   footprints). Per-library lazy loading is 0.1–1.3 s and 1–270 MB per library, but no unload exists
   (`ReloadLibrary` only) and the footprint handler is only registered once the board is open or `load_all_libraries(footprints)` ran.
3. **Pins of derived symbols are not in the API response** (54 % of stock symbols, incl. MCUs,
   4xxx/74xx variants). The dropped-commit probe placement recovers them in ~22 ms. This depends on
   2A confirming `drop_commit` semantics and is the only place where a lookup needs a (discarded) write.
4. `PlaceSymbolFromLibrary` needs the library loaded; `load_all_libraries` right after open (spec
   §4) satisfies this for symbols and registers the footprint handler.
5. Coordinates from the API are y-down, relative to the symbol origin, unrotated; multi-body-style
   symbols repeat pins per body style (dedupe by number/unit).

## Decision
**2B partial: derived (`extends`) symbols carry no pins in the listing/detail fetch → fetch pins lazily
per symbol through a dropped-commit probe placement; footprint details (pad count, description) may also
be fetched lazily/in the background; everything else (name, description, keywords, filters, default
footprint, unit/body-style counts, project libraries, search < 300 ms) is eager and API-only.**
Memory (~4.9 GB for the server with all libraries) is flagged to the coordinator as a deployment
decision, not a blocker.

## Follow-ups for the implementation phases
- `lib_search_symbols` rows: `pin_count` is `null` for derived symbols until inspected; fill it lazily
  (probe, cache by `lib_id`) or in a background pass (12 313 × 22 ms ≈ 4.5 min) — do not block startup.
- `lib_get_symbol` for derived symbols: probe placement in `begin_commit`/`drop_commit`; add a test that
  the document is unchanged and unmodified (relies on 2A).
- Warm-up order in `KicadSession`: open sch + board → `load_all_libraries` → `wait_for_libraries(timeout ≥ 120 s)`
  → list → batched detail fetch (symbols first, footprints in background) → index. Search before
  ready returns a structured "libraries still loading" error. Never request footprints before the
  board is open or footprints were load-all'ed.
- Decide the memory budget: full stock set ≈ 5 GB per project server. Options: accept, or load only the
  symbol libraries (1.35 GB) plus footprint libraries on demand per nickname (needs a footprint-name
  source; search by name for footprints then only covers loaded libraries). Needs the owner's
  deployment target.
- Merge order "project libraries first" in the index (`scope` from `get_library_statuses`); fall back to the
  name when description/keywords are empty.
- Part preference check: `package_of(footprint name)` + 2 SMD pads (+ description cross-check); unknown →
  no deviation flagged. Reuse `spikes/2b-libraries/libdata.py` / `index.py` as the starting point.
- Not tested: library-table changes while running (`reload_library`), nickname collisions between
  global and project tables, broken table rows (status carries `error_message`).
