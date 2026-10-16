#!/usr/bin/env bash
# Copies KiCad demo projects verbatim and upgrades them with kicad-cli only.
set -euo pipefail
cd "$(dirname "$0")"
DEMOS="${KICAD_DEMOS:-$HOME/build/kicad/demos}"
CLI="${KICAD_CLI:-$PWD/.kicad/squashfs-root/usr/bin/kicad-cli}"
APPDIR="$PWD/.kicad/squashfs-root"
if [ -d "$APPDIR" ]; then
  export LD_LIBRARY_PATH="$APPDIR/usr/lib:$APPDIR/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
fi
export LANG=C LC_ALL=C
rm -rf fixtures && mkdir -p fixtures/ecc83 fixtures/complex_hierarchy
# Only the ecc83-pp project (the demo dir also holds ecc83-pp_v2).
cp "$DEMOS"/ecc83/ecc83-pp.kicad_* fixtures/ecc83/
for f in fp-lib-table sym-lib-table; do
  [ -e "$DEMOS/ecc83/$f" ] && cp "$DEMOS/ecc83/$f" fixtures/ecc83/
done
[ -d "$DEMOS/ecc83/footprints.pretty" ] && cp -r "$DEMOS/ecc83/footprints.pretty" fixtures/ecc83/
cp -r "$DEMOS"/complex_hierarchy/. fixtures/complex_hierarchy/
for sch in fixtures/*/*.kicad_sch; do "$CLI" sch upgrade --force "$sch"; done
for pcb in fixtures/*/*.kicad_pcb; do "$CLI" pcb upgrade --force "$pcb"; done
ls -R fixtures | head -n 60
