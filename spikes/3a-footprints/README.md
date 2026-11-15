# Spike 3A — footprint library via the KiCad API

Throwaway. Not run in CI. Findings: `docs/spikes/3a-footprints.md`.

Runs inside the pinned image `ghcr.io/peterus/copperchat-kicad-base:52b43fb8…-rf96c7c8335ad9c6f`
(`kicad-cli` + kipy + stock libraries; override with `KICAD_BASE_IMAGE`). Needs docker only. If the
image still ships upstream kipy `76b47d25`, `run.sh` overlays `.deps/kipy` = that kipy + the fork's
#147 fix (`ab07e98`).

```
./run_all.sh              # all questions, results/summary.md + results/*.json|log
./run.sh q01_create.py    # one question
```

| File | Question |
|---|---|
| `q01_create.py` | Q1 + acceptance case L201: seed → `SaveCopyOfDocument` → rebuild → save → read back, footprint-level attributes, `fp upgrade` round trip |
| `q02_library.py` | Q2: library-table commands, missing/empty `.pretty`, `ReloadLibrary`, overwrite, no delete |
| `q03_shared.py` | Q3: one library at the repo root, two projects, two api-servers |
| `q04_coexist.py` | Q4: footprint document next to schematic + board, interleaved commits, document bookkeeping |
| `q05_read.py` | Q5: reading footprints without a board (library handler, batch, document route, RSS) |
| `q06_kipy.py` | Q6: which kipy wrappers work on a footprint document |
| `q07_assign.py` | Q7: Footprint field, ERC `footprint_link_issues`, board placement |
| `q08_render.py` | Q8: `kicad-cli fp export svg`, scale and per-layer extents |
| `explore4.py` | board library cache variants (board opened before/after the create, schematic save) |
| `fpdoc.py` | raw-protobuf footprint document client (prototype for CopperChat) |
| `l201.py` | acceptance case as protobuf items + geometry read-back |
| `repo.py` | synthetic repo (projects created by KiCad) + the spike-only `fp-lib-table` stand-in |
| `steps.py`, `harness.py` | shared steps, api-server launcher, recorder |

Every KiCad file is written by KiCad (`CreateDocument`, `SaveDocument`, `SaveCopyOfDocument`), except
the project `fp-lib-table` rows: the API has no handler for them (Q2), so `repo.standin_fp_lib_table`
writes them into the throwaway tmp repo. `results/copperchat.pretty/` is the footprint KiCad wrote in
the last `q01` run.
