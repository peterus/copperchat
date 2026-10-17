# Spike 1B — KiCanvas with KiCad 11 files

Throwaway code. Results: `docs/spikes/1b-kicanvas.md`. CI never runs this.

    ./fetch_kicanvas.sh && ./check_vendor.sh          # build KiCanvas at KICANVAS_COMMIT
    ./fetch_kicad_nightly.sh                           # nightly AppImage → .cache/bin/kicad-cli
    ./prepare_files.sh && python3 verify_files.py      # copy demos, upgrade with kicad-cli
    npm ci && npx playwright install chromium
    npx playwright test --reporter=list                # results/ (jsonl, screenshots)
    python3 summarize.py results                       # results/summary.md

Supplementary (not the gate): KiCanvas with the KiCad 11 footprint patch.

    KICANVAS_PATCH=patches/kicad11-footprint-transform.patch VENDOR_DIR=vendor-patched ./fetch_kicanvas.sh
    KICANVAS_VENDOR=vendor-patched npx playwright test --reporter=list   # results-patched/
    python3 summarize.py results-patched

KiCad files under `files/` are produced only by `kicad-cli … upgrade`; never edit them.

Evaluation demo (patched KiCanvas + KiCad 11 demo projects, served on :8765): `demo/README.md`.
