# Spike 2B — library access via the KiCad API

Throwaway. Not run in CI. Findings: `docs/spikes/2b-libraries.md`.

Runs inside the pinned image `ghcr.io/peterus/copperchat-kicad-base:0616634034fa1a8c92c141c293342cbef2f68dff`
(`kicad-cli` + kipy `87b945a0` + stock libraries; override with `KICAD_BASE_IMAGE`). Needs docker only.

```
./run_all.sh              # all questions, results/summary.md + results/*.json|log
./run.sh q05_index.py     # one question
```

| File | Question |
|---|---|
| `q01_load.py` | `load_all_libraries` time + RSS (all / symbols only / footprints only), per-library lazy cost, what works without `load_all` |
| `q02_symbols.py` | `GetLibraryItems` payload, `GetItemsFromLibrary` for symbols (pins, units, body styles, fields), derived symbols + probe placement |
| `q03_footprints.py` | footprint details (description, pads), package (0805) derivation |
| `q04_project_libs.py` | project `sym-lib-table`/`fp-lib-table` with `${KIPRJMOD}` (four stock demo projects) |
| `q05_index.py` | warm-up timeline and in-memory search index (build, latency, relevance) |
| `libdata.py`, `index.py` | prototypes of the data-access layer / index the implementation will need |
| `harness.py` | api-server launcher (mirrors `KicadCliLauncher`), recorder |

Fixtures are byte copies of stock demo projects from the image (`ecc83`, `cm5_minima`, `pic_programmer`,
`interf_u`); nothing is hand-edited. Wall-clock numbers vary with host load (the host also ran other
jobs): the load time was 7–23 s across runs.
