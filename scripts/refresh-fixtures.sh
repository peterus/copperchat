#!/usr/bin/env bash
# Copies KiCad demo projects verbatim and upgrades them with kicad-cli.
# Never edit the results by hand.
#
# Run inside the KiCad base image (ghcr.io/peterus/copperchat-kicad-base:<KICAD_COMMIT>),
# whose /usr/share/kicad/demos is installed from the pinned KiCad commit:
#   scripts/refresh-fixtures.sh [DEMOS_DIR]   (default: /usr/share/kicad/demos)
set -euo pipefail
DEMOS="${1:-/usr/share/kicad/demos}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMMIT="$(tr -d '[:space:]' < "$ROOT/docker/kicad-base/KICAD_COMMIT")"
DEST="$ROOT/backend/tests/fixtures/projects"
[ -d "$DEMOS/ecc83" ] && [ -d "$DEMOS/complex_hierarchy" ] || {
  echo "no KiCad demos in $DEMOS" >&2; exit 1; }

rm -rf "$DEST"
mkdir -p "$DEST/ecc83" "$DEST/complex_hierarchy"

# ecc83: only the ecc83-pp project (a second .kicad_pro in one directory would
# make the project ambiguous), its library tables and project-local libraries.
cp "$DEMOS"/ecc83/ecc83-pp.* "$DEST/ecc83/"
cp "$DEMOS"/ecc83/sym-lib-table "$DEMOS"/ecc83/fp-lib-table "$DEST/ecc83/"
cp -r "$DEMOS"/ecc83/footprints.pretty "$DEST/ecc83/"
cp -r "$DEMOS"/complex_hierarchy/. "$DEST/complex_hierarchy/"
find "$DEST" \( -name '*-backups' -o -name '*.lck' -o -name '*.kicad_prl' \) -prune \
  -exec rm -rf {} +

export LANG=C LC_ALL=C
find "$DEST" -name '*.kicad_sch' -print0 | while IFS= read -r -d '' f; do
  kicad-cli sch upgrade --force "$f"
done
find "$DEST" -name '*.kicad_pcb' -print0 | while IFS= read -r -d '' f; do
  kicad-cli pcb upgrade --force "$f"
done
# kicad-cli writes .kicad_prl (local view state) while upgrading; not part of a fixture.
find "$DEST" \( -name '*-backups' -o -name '*.lck' -o -name '*.kicad_prl' \) -prune \
  -exec rm -rf {} +
echo "$COMMIT" > "$DEST/KICAD_COMMIT"
echo "fixtures refreshed from KiCad $COMMIT"
