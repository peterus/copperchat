# Spike 1B — KiCanvas with KiCad 11 files

Throwaway code. Results: `docs/spikes/1b-kicanvas.md`. CI never runs this.

    ./fetch_kicanvas.sh && ./check_vendor.sh          # build KiCanvas at KICANVAS_COMMIT
    ./fetch_kicad_nightly.sh                           # nightly AppImage → .cache/bin/kicad-cli
    ./prepare_files.sh && python3 verify_files.py      # copy demos, upgrade with kicad-cli
    npm ci && npx playwright install chromium
    npx playwright test --reporter=list                # results/ (jsonl, screenshots)

KiCad files under `files/` are produced only by `kicad-cli … upgrade`; never edit them.
