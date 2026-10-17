#!/usr/bin/env bash
# Whole spike: pairs via the IPC API, kicad-cli sch diff in all formats, cross-check, timing,
# patched KiCanvas build, Playwright. Needs docker (pinned kicad-base image), node, python3.
set -euo pipefail
cd "$(dirname "$0")"
[ -d node_modules ] || npm ci --include=dev --no-audit --no-fund --loglevel=error
./run.sh harness/make_pairs.py
./run.sh harness/collateral.py
./run.sh harness/probe_subsheet_update.py
./run.sh harness/run_diff.py
./run.sh harness/timing.py
python3 harness/crosscheck.py .work/pairs
mkdir -p results/samples
for d in .work/pairs/*/; do n=$(basename "$d"); mkdir -p "results/samples/$n"; cp "$d"/diff.json "$d"/diff.text "$d"/diff.svg "$d"/diff.png "$d"/changelog.json "results/samples/$n/"; done
cp .work/pairs/diff_runs.json results/
./build_patched.sh
npx playwright test --reporter=list
