#!/usr/bin/env bash
# Copy KiCad demo projects and upgrade them to the nightly (KiCad 11) format with kicad-cli.
# Never edit the resulting files by hand.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CLI="${KICAD_CLI:-$HERE/.cache/bin/kicad-cli}"
SRC="${KICAD_DEMOS:-$HOME/build/kicad/demos}"
DST="${KICAD_FILES_DIR:-$HERE/files}"
DEMOS="${KICAD_DEMO_LIST:-ecc83 complex_hierarchy}"
mkdir -p "$HERE/.cache"
"$CLI" sch upgrade --help | tee "$HERE/.cache/sch-upgrade-help.txt" >/dev/null
force=""
grep -q -- '--force' "$HERE/.cache/sch-upgrade-help.txt" && force="--force"
rm -rf "$DST" && mkdir -p "$DST"
for demo in $DEMOS; do
  cp -r "$SRC/$demo" "$DST/$demo"
  for f in "$DST/$demo"/*.kicad_sch; do "$CLI" sch upgrade $force "$f"; done
  for f in "$DST/$demo"/*.kicad_pcb; do "$CLI" pcb upgrade $force "$f"; done
done
ls -la "$DST"/*
