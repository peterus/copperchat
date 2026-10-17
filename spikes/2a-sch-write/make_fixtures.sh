#!/usr/bin/env bash
# Run inside the kicad-base image (./run.sh ./make_fixtures.sh). Copies KiCad demo
# projects verbatim and upgrades them with kicad-cli; creates the "blank" project
# through the API (CreateDocument). Never hand-edits a KiCad file.
set -euo pipefail
cd "$(dirname "$0")"
DEMOS="${KICAD_DEMOS:-/usr/share/kicad/demos}"
export LANG=C LC_ALL=C KICAD_CONFIG_HOME="$PWD/.kicad/config"
rm -rf fixtures && mkdir -p fixtures/ecc83 fixtures/complex_hierarchy
cp "$DEMOS"/ecc83/ecc83-pp.kicad_* "$DEMOS"/ecc83/sym-lib-table "$DEMOS"/ecc83/fp-lib-table fixtures/ecc83/
cp -r "$DEMOS"/ecc83/footprints.pretty fixtures/ecc83/
cp -r "$DEMOS"/complex_hierarchy/. fixtures/complex_hierarchy/
for sch in fixtures/*/*.kicad_sch; do kicad-cli sch upgrade --force "$sch"; done
/opt/venv/bin/python make_blank.py
ls -R fixtures | head -n 40
