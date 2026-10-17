#!/usr/bin/env bash
# Build everything the static demo needs (all outputs are gitignored):
#   vendor/   KiCanvas at KICANVAS_COMMIT + patches/*.patch
#   files/    KiCad demo projects upgraded to the KiCad 11 format by kicad-cli
#             (prepare_files.sh run inside the pinned base image; never hand-edited)
#   fonts/    self-hosted Nunito + Material Symbols Outlined (no Google Fonts request)
#   projects.json  manifest for the project picker
# Usage: ./build.sh [kicanvas|files|fonts|manifest]...   (default: all)
set -euo pipefail
DEMO="$(cd "$(dirname "$0")" && pwd)"
SPIKE="$(dirname "$DEMO")"
IMAGE="${KICAD_IMAGE:-ghcr.io/peterus/copperchat-kicad-base:0616634034fa1a8c92c141c293342cbef2f68dff}"
PROJECTS="${DEMO_PROJECTS:-ecc83 complex_hierarchy pic_programmer video jetson-agx-thor-baseboard}"
PATCHES="patches/kicad11-footprint-transform.patch patches/embed-select-event.patch patches/embed-project-getter.patch patches/no-external-fonts.patch"

build_kicanvas() {
  (cd "$SPIKE" && KICANVAS_PATCH="$PATCHES" VENDOR_DIR=demo/vendor ./fetch_kicanvas.sh)
  if grep -q 'fonts.googleapis.com' "$DEMO/vendor/kicanvas.js"; then
    echo "FAIL: bundle still references fonts.googleapis.com"; exit 1
  fi
}

build_files() {
  # kicad-cli from the pinned base image upgrades copies of the image's own demos.
  docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -e KICAD_CLI=kicad-cli -e KICAD_DEMOS=/usr/share/kicad/demos \
    -e KICAD_FILES_DIR=/spike/demo/files -e KICAD_DEMO_LIST="$PROJECTS" \
    -v "$SPIKE:/spike" --entrypoint /spike/prepare_files.sh "$IMAGE"
}

build_fonts() {
  local tmp="$SPIKE/.cache/fonts"
  rm -rf "$tmp" && mkdir -p "$tmp" "$DEMO/fonts"
  (cd "$tmp" && npm pack --silent @fontsource/nunito@5 material-symbols@0.48.0 >/dev/null)
  for t in "$tmp"/*.tgz; do mkdir -p "${t%.tgz}" && tar -xzf "$t" -C "${t%.tgz}"; done
  local nunito="$tmp"/fontsource-nunito-*/package material="$tmp"/material-symbols-*/package
  for w in 400 600 700; do cp $nunito/files/nunito-latin-$w-normal.woff2 "$DEMO/fonts/"; done
  cp $nunito/LICENSE "$DEMO/fonts/LICENSE-Nunito-OFL.txt"
  cp $material/material-symbols-outlined.woff2 "$DEMO/fonts/"
  cp $material/LICENSE "$DEMO/fonts/LICENSE-MaterialSymbols-Apache2.txt"
}

build_manifest() {
  python3 "$DEMO/make_manifest.py" "$DEMO/files" > "$DEMO/projects.json"
  cat "$DEMO/projects.json"
}

steps=("$@"); [ ${#steps[@]} -eq 0 ] && steps=(kicanvas files fonts manifest)
for s in "${steps[@]}"; do "build_$s"; done
