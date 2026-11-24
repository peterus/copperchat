# Spike 4A — board (.kicad_pcb) capabilities via the KiCad API, kicad-cli and Freerouting

Throwaway. Not run in CI. Findings: `docs/spikes/4a-board.md`.

Runs inside the pinned image `ghcr.io/peterus/copperchat-kicad-base:52b43fb8…-r98c78374f0daab9d`
(patched KiCad + kipy fork; override with `KICAD_BASE_IMAGE`). Host needs docker, `gh` and ImageMagick
`convert` (rasterises the board SVGs, the image has no rasteriser).

```
./fetch_odh.sh            # once: copy of OpenDriveHub transmitter main -> fixtures/odh (gitignored, read-only source)
./fetch_deps.sh           # once: Freerouting 2.5.0 JAR + Java 25 runtime -> .deps (gitignored)
./run_all.sh              # all questions, results/summary.md + results/*.json|log|png|csv|zip (~20 min)
./run.sh q04_route.py     # one question
```

| File | Question |
|---|---|
| `q01_netlist.py` | Q1 schematic -> board: netlist export, `ImportNetlist`, local library, sheet info, re-sync |
| `q02_setup.py` | Q2 outline, mounting holes, 2<->4 layers, stackup, design rules, net classes, custom rules, keepout |
| `q03_place.py` | Q3 placement queries/speed + the generic placer (`place.py`) on ecc83 and OpenDriveHub |
| `q04_route.py` | Q4a tracks/vias/arcs via API + DRC feedback + interactive router; Q4b Freerouting round trip |
| `q04c_astar.py` | Q4c own octilinear A* (`router.py`) for short nets, then Freerouting for the rest |
| `q05_zones.py` | Q5 GND planes, pours, refill, fanout (`fanout.py`), stitching vias, DRC API vs CLI |
| `q06_metrics.py` | Q6 quality metrics (`metrics.py`): human ecc83 vs ours, decoupling, compactness |
| `q07_show.py` | Q7 per-layer/composite images, 3D render, STEP/GLB, in-memory board string |
| `q08_fab.py` | Q8 JLCPCB outputs: Gerbers + paste, drill, CPL, BOM, zip |
| `boards.py`, `flow.py` | test boards (ecc83 demo with a KiCad-created empty board; OpenDriveHub copy), board setup, render |
| `place.py`, `router.py`, `fanout.py`, `metrics.py`, `drc.py`, `freerouting.py` | prototypes used by the probes |
| `harness.py` | api-server launcher + recorder (from spike 3A) |

Every KiCad file is written by KiCad (`CreateDocument`, `ImportNetlist`, API edits, `SaveDocument`) or is a
plain copy of a KiCad-written file. Placed/routed boards live in tmp dirs only; `results/` keeps renders,
JSON evidence and the fab CSVs/zips of the synthetic runs.
